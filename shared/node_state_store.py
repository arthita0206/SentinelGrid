"""Persists the CURRENT (latest-known) state of one node to the
SentinelGridNodeState DynamoDB table (Step 34 — SRS FR-6.1).

This is a SHARED AWS-facing module — used by both
`lambda_functions/anomaly_detector/handler.py` (the Analyzer) and
`lambda_functions/remediation/{planner,executor,verifier}.py` — exactly
mirroring `shared/metrics.py`'s own cross-package sharing pattern (Step
28). It is NOT a copy of `lambda_functions/anomaly_detector/event_store.py`'s
per-package convention: `event_store.py`/`recovery_event_store.py` each
belong to exactly one package and model a genuinely different concept
(APPEND-only historical events with a `(node_id, timestamp)` key) — this
module models a single, mutable, LATEST-STATE row per node
(`node_id`-only key), read and written the same way from every stage of
the pipeline that has an opinion about it.

**This is current-state storage, not event history.** Every write here
REPLACES (conditionally — see "Ordering" below) the one row for a given
`node_id`; nothing here ever appends a new row for the same node.
Contrast with `SentinelGridAnomalyEvents`/`SentinelGridRecoveryEvents`,
which are append-only and keyed by `(node_id, timestamp)`.

**Ordering / stale-write protection**: every write is conditioned on
`attribute_not_exists(node_id) OR :event_timestamp >= last_telemetry_timestamp`.
This single condition uniformly protects two related but distinct
scenarios with the same mechanism:
  1. The Analyzer processing telemetry out of order (a late-arriving
     older message must never regress a newer one) — it passes the
     CURRENT telemetry event's own timestamp as `:event_timestamp`.
  2. A remediation Lambda (Planner/Executor/Verifier) writing a
     RECOVERING/RECOVERED/ESCALATION_REQUIRED transition for the fault
     it is processing — it passes the ORIGINATING detection's timestamp
     (`detection_result.timestamp`, carried unchanged through the
     workflow) as `:event_timestamp`. If a genuinely NEWER telemetry
     event has already moved `last_telemetry_timestamp` past that value
     (e.g. the node came back healthy on its own before this remediation
     workflow finished), the remediation write is correctly rejected as
     stale rather than clobbering the newer, more current telemetry-driven
     state — exactly the "old recovery result must not overwrite a newer
     fault [or newer healthy state]" protection Step 34 requires.
The `>=` (not strict `>`) additionally makes a retried write for the
EXACT SAME timestamp an idempotent, harmless overwrite (the recomputed
values are identical for a deterministic pipeline) rather than a
rejection — see `update_state()`'s own docstring.

**Failure semantics — deliberately BEST-EFFORT, not fail-fast** (a
documented departure from `event_store.py`/`recovery_event_store.py`'s
fail-fast convention): current state is a derived, correctable snapshot
— a missed update is superseded by the next telemetry event or workflow
step, unlike a lost anomaly/recovery EVENT, which is gone forever with
no "next chance" to record that specific historical fact. Making this
best-effort also directly eliminates the exact risk Step 34 warns about
("a state-persistence failure must not cause an Analyzer retry that
creates duplicate anomaly records or duplicate remediation executions")
by construction: since this module never raises, it can never trigger a
Lambda retry in the first place. Every failure is logged at ERROR (not
WARNING, unlike `shared/metrics.py`'s best-effort metric publication) —
loud enough that "current state" is never silently and incorrectly
assumed to be up to date.
"""

import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # see anomaly_detector/handler.py for why this is necessary on Lambda

NODE_STATE_TABLE_NAME_ENV_VAR = "NODE_STATE_TABLE_NAME"
DEFAULT_TABLE_NAME = "SentinelGridNodeState"

_PARTITION_KEY = "node_id"


class NodeState(str, Enum):
    """The fixed, closed set of current-state values (Step 34). Not the
    same vocabulary as `remediation.models.ExecutionStatus` or
    `lambda_functions.remediation.recovery_event_store.RecoveryStatus` —
    this describes the node's own lifecycle, not one Lambda's outcome,
    though `ESCALATION_REQUIRED` is deliberately spelled identically to
    `RecoveryStatus.ESCALATION_REQUIRED` since it is the same real-world
    condition viewed from a different table.
    """

    HEALTHY = "HEALTHY"
    FAULT_DETECTED = "FAULT_DETECTED"
    RECOVERING = "RECOVERING"
    RECOVERED = "RECOVERED"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    UNKNOWN = "UNKNOWN"


class NodeStateStore:
    """Best-effort writer for the SentinelGridNodeState table.

    Accepts an optional pre-built DynamoDB Table resource (`table=`) for
    dependency injection — tests pass a fake/mock here, so no real AWS
    call is ever made during the test suite, mirroring
    `AnomalyEventStore`'s/`RecoveryEventStore`'s own `table=` injection
    pattern. `table_name` resolution order: explicit argument, else the
    `NODE_STATE_TABLE_NAME` environment variable, else `DEFAULT_TABLE_NAME`.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        table: Optional[Any] = None,
    ):
        self.table_name = table_name or os.environ.get(NODE_STATE_TABLE_NAME_ENV_VAR, DEFAULT_TABLE_NAME)
        self.region = region
        self.profile = profile
        self._table = table

    def _get_table(self):
        if self._table is None:
            if self.profile:
                session = boto3.Session(profile_name=self.profile, region_name=self.region)
            else:
                session = boto3.Session(region_name=self.region)
            self._table = session.resource("dynamodb", region_name=self.region).Table(self.table_name)
        return self._table

    def update_state(
        self,
        *,
        node_id: Optional[str],
        event_timestamp: Optional[str],
        state: str,
        status: Optional[str] = None,
        subsystem: Optional[str] = None,
        fault_type: Optional[str] = None,
        fault_severity: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Atomically writes (creates or updates) the one current-state
        row for `node_id`, conditioned on `event_timestamp` being the
        latest this store has seen for this node — see module docstring
        "Ordering / stale-write protection".

        `fault_type`/`fault_severity` of `None` REMOVE those attributes
        (a HEALTHY/RECOVERED node has no current fault) rather than
        writing a `null` — DynamoDB-idiomatic and matches this project's
        "absent, not null" convention for optional fields.

        Returns `True` if the write was applied, `False` for every other
        outcome (stale/duplicate timestamp rejected, or a genuine AWS
        failure) — NEVER raises. See module docstring "Failure semantics".
        Callers must not treat a `False` return as fatal; they may log it
        for visibility, which is already done here at ERROR/INFO level.
        """
        if not node_id or not event_timestamp:
            logger.error(
                "SentinelGrid node state: skipping update -- missing usable node_id/event_timestamp "
                "(node_id=%r, event_timestamp=%r).",
                node_id,
                event_timestamp,
            )
            return False

        # Built directly, field by field, rather than via post-hoc string
        # manipulation: DynamoDB rejects an UpdateItem call if
        # ExpressionAttributeNames/Values contains ANY entry not actually
        # referenced by the expression, so every name/value pair added
        # below is added in lockstep with the expression clause that
        # references it -- never added speculatively then pruned.
        set_parts = ["#state = :state", "#last_telemetry_timestamp = :event_timestamp", "#last_state_update = :now"]
        remove_parts = []
        names = {
            "#state": "state",
            "#last_telemetry_timestamp": "last_telemetry_timestamp",
            "#last_state_update": "last_state_update",
        }
        values = {
            ":state": state,
            ":event_timestamp": event_timestamp,
            ":now": datetime.now(timezone.utc).isoformat(),
            ":check_event_timestamp": event_timestamp,
        }

        # subsystem/status/reason: SET when present, otherwise simply
        # never mentioned in the expression at all (no REMOVE needed --
        # an attribute that was never set stays absent; one already set
        # from a prior write is deliberately left as-is rather than
        # cleared, since these three are never meaningfully "cleared" by
        # this project's own state transitions the way fault_type/
        # fault_severity are).
        for attr, value in (("subsystem", subsystem), ("status", status), ("reason", reason)):
            if value is not None:
                set_parts.append(f"#{attr} = :{attr}")
                names[f"#{attr}"] = attr
                values[f":{attr}"] = value

        if fault_type is not None and fault_severity is not None:
            set_parts.append("#fault_type = :fault_type")
            set_parts.append("#fault_severity = :fault_severity")
            names["#fault_type"] = "fault_type"
            names["#fault_severity"] = "fault_severity"
            values[":fault_type"] = fault_type
            values[":fault_severity"] = fault_severity
        else:
            remove_parts.append("#fault_type")
            remove_parts.append("#fault_severity")
            names["#fault_type"] = "fault_type"
            names["#fault_severity"] = "fault_severity"

        update_expression = "SET " + ", ".join(set_parts)
        if remove_parts:
            update_expression += " REMOVE " + ", ".join(remove_parts)

        table = self._get_table()
        try:
            table.update_item(
                Key={_PARTITION_KEY: node_id},
                UpdateExpression=update_expression,
                ConditionExpression=(
                    "attribute_not_exists(#pk) OR :check_event_timestamp >= #last_telemetry_timestamp"
                ),
                ExpressionAttributeNames={**names, "#pk": _PARTITION_KEY},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code == "ConditionalCheckFailedException":
                logger.info(
                    "SentinelGrid node state: skipped update for node_id=%s -- an equal-or-newer "
                    "telemetry timestamp (>= %s) is already recorded (stale write or idempotent retry).",
                    node_id,
                    event_timestamp,
                )
                return False
            logger.error(
                "SentinelGrid node state: failed to update node_id=%s to state=%s: %s",
                node_id,
                state,
                exc,
            )
            return False
        except BotoCoreError as exc:
            logger.error(
                "SentinelGrid node state: failed to update node_id=%s to state=%s: %s",
                node_id,
                state,
                exc,
            )
            return False

        logger.info("SentinelGrid node state: node_id=%s updated to state=%s", node_id, state)
        return True
