"""Persists recovery/escalation outcomes to the SentinelGridRecoveryEvents
DynamoDB table (Step 33 — SRS Knowledge Base persistent recovery history).

Mirrors `lambda_functions/anomaly_detector/event_store.py`'s pattern
exactly: lazy boto3, `table=` dependency injection, Decimal-safe
conversion, a conditional idempotent `PutItem`, fail-fast on genuine
errors. This is the one place in the remediation Lambda packages that
talks to this table — `remediation/engine.py` and `remediation/models.py`
remain completely AWS-independent, exactly as before Step 33.

This is recovery-EVENT HISTORY, not a current-state model: each item is
one immutable, historical record of what happened for one
(node_id, timestamp) detection — never updated in place, never treated
as "the current status of this node." See infrastructure/README.md.

**What gets built, and by whom** (mutually exclusive per event, by
construction of the existing, unmodified ASL — mirrors Step 28's
UnresolvedFaultCount ownership split exactly):

- `build_escalation_event()` is called by `planner.py`, only when it
  decides `escalation_required=True` for a fault that needs remediation.
  `step_functions/remediation_workflow.json`'s own `CheckAutomaticRemediation`
  Choice state routes straight to `Escalate` for that case — `executor.py`
  is never invoked for the same node_id/timestamp, so there is no
  possibility of a second write to the same key from that path.
- `build_execution_event()` is called by `executor.py`, for every plan it
  actually receives — which, per that same ASL routing, always has
  `automatic=True` (so `planner.py` never wrote for this key either).
  Returns `None` for `ExecutionStatus.SKIPPED` (nothing was required —
  nothing worth recording as a recovery event).

**Idempotency**: the natural primary key is `(node_id, timestamp)` — the
DETECTION timestamp (matching `SentinelGridAnomalyEvents`' own sort key),
not a fresh wall-clock value — so a retried Lambda invocation for the
same underlying detection always produces the identical key. A
conditional `PutItem` (`attribute_not_exists` on both key attributes)
makes a true retry an idempotent no-op, exactly like `event_store.py`.
This module goes one step further: on a `ConditionalCheckFailedException`,
it reads the existing item back (`GetItem`, consistent read — the one
reason this module needs that permission beyond `PutItem`) and compares
the fields that actually describe the OUTCOME (`status`, `success`,
`automatic`, `action`, `fault_type`, `severity`). If they match, this is
a genuine retry — treated as success, logged, no duplicate created. If
they differ, two different recovery outcomes are claiming the same
identity — `RecoveryEventConflictError` is raised rather than silently
keeping whichever record landed first or overwriting it with the new
one. Historical evidence is never destroyed.

**Failure semantics**: persistence failures (including conflicts) are
NEVER swallowed — `put_event()` raises `RecoveryEventPersistenceError`
(or its `RecoveryEventConflictError` subclass), and callers
(`planner.py`/`executor.py`) let this propagate, matching
`event_store.py`'s own fail-fast policy exactly. This does NOT change
`RemediationPlan.to_dict()`/`RemediationResult.to_dict()`'s external
response shape in any way — those describe the remediation DECISION, not
persistence status, and never have. A persistence failure fails the
Lambda invocation itself (visible in CloudWatch and to Step Functions),
rather than ever returning a plan/result as if its history had been
durably recorded when it was not.

**Known, deliberate consequence for `PlanRemediation` specifically**:
unlike `ExecuteRemediation`/`VerifyRemediation`, the `PlanRemediation`
Task in the existing (unmodified) ASL has no `Catch` block — a raised
exception there fails that Step Functions execution outright (visible as
`FAILED`) rather than gracefully routing to `Escalate`. Step 33
deliberately does not add a Catch (that would be an ASL change) and does
not swallow the error either (that would risk silently losing escalation
history). This is a considered trade-off, consistent with this project's
established fail-fast persistence convention (every other AWS-facing
adapter in this codebase — `event_store.py`, `remediation_trigger_client.py`
— behaves the same way): a lost escalation record is treated as a loud,
visible failure, never a silent one.
"""

import decimal
import logging
import math
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from remediation.models import ExecutionStatus, RemediationPlan, RemediationResult

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # see anomaly_detector/handler.py for why this is necessary on Lambda

RECOVERY_EVENTS_TABLE_NAME_ENV_VAR = "RECOVERY_EVENTS_TABLE_NAME"
DEFAULT_TABLE_NAME = "SentinelGridRecoveryEvents"

# The DynamoDB item's partition/sort key attribute names, matching
# infrastructure/recovery-events-storage.yaml's KeySchema exactly.
_PARTITION_KEY = "node_id"
_SORT_KEY = "timestamp"

# Fields compared on a duplicate-key hit to distinguish a genuine retry
# (idempotent no-op) from a real conflict. Deliberately excludes
# `recorded_at` (always different — it's the wall-clock write time) and
# free-text `reason` (may legitimately be reworded without describing a
# different OUTCOME) — see module docstring "Idempotency".
_CONFLICT_COMPARISON_FIELDS = ("status", "success", "automatic", "action", "fault_type", "severity")


class RecoveryStatus(str, Enum):
    """The fixed, closed set of recovery-event outcomes (Step 33)."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"


class PersistenceOutcome(str, Enum):
    """What `RecoveryEventStore.put_event()` actually did — both are
    successful outcomes; mirrors `event_store.PersistenceOutcome` exactly.
    """

    NEWLY_PERSISTED = "NEWLY_PERSISTED"
    DUPLICATE_ALREADY_EXISTS = "DUPLICATE_ALREADY_EXISTS"


class RecoveryEventPersistenceError(RuntimeError):
    """Raised when a recovery event could not be written to DynamoDB."""


class RecoveryEventConflictError(RecoveryEventPersistenceError):
    """Raised when an item already exists for (node_id, timestamp) with
    MATERIALLY DIFFERENT outcome data — see module docstring
    "Idempotency". The existing record is never overwritten or deleted.
    """


class InvalidRecoveryEventError(ValueError):
    """Raised when an item passed to `RecoveryEventStore.put_event()` is
    missing a usable `node_id`/`timestamp` — this table's primary key —
    before any DynamoDB call is attempted. `build_escalation_event()`/
    `build_execution_event()` only ever produce a usable key when their
    own inputs (`RemediationPlan`/`RemediationResult`) already have one,
    so this guards against a malformed item reaching this store some
    other way, rather than surfacing an opaque `KeyError`/DynamoDB
    validation error instead.
    """


def _to_dynamodb_value(value: Any) -> Any:
    """Identical conversion rules to
    `lambda_functions/anomaly_detector/event_store.py`'s own helper —
    see that module's docstring for the full reasoning (float ->
    Decimal, NaN/Infinity -> str, tuple -> list, recursive on dict/list).
    Kept as an independent copy rather than a shared import: each Lambda
    package in this project is deployed as its own self-contained zip
    (see infrastructure/package-remediation-lambdas.sh), so sharing this
    small helper across packages would couple their deployment artifacts
    together for no benefit — the same reasoning
    `remediation_trigger/handler.py` already documents for its own
    independent copy of `_extract_detection_result`.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return decimal.Decimal(str(value))
    if isinstance(value, dict):
        return {key: _to_dynamodb_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamodb_value(val) for val in value]
    return value


def _build_base_item(
    *,
    node_id: Optional[str],
    timestamp: Optional[str],
    subsystem: Optional[str],
    fault_type: Optional[str],
    severity: Optional[str],
    action: str,
    automatic: bool,
    status: str,
    success: bool,
    reason: str,
) -> dict:
    """Builds the common recovery-event item shape. `timestamp` here is
    the underlying DETECTION timestamp (this table's sort key — "the
    timestamp should represent the recovery event associated with the
    DetectionResult"), not the wall-clock moment this Lambda ran;
    `recorded_at` separately captures that wall-clock write time.
    `recovery_started_at` is set equal to the detection timestamp too —
    the only "recovery start" reference point available without adding a
    new Step Functions Context Object field (an ASL change Step 33
    deliberately avoids), matching how Step 28's own `RecoveryLatency`
    metric already measures from this same field.
    """
    return {
        _PARTITION_KEY: node_id,
        _SORT_KEY: timestamp,
        "subsystem": subsystem,
        "fault_type": fault_type,
        "severity": severity,
        "action": action,
        "automatic": automatic,
        "status": status,
        "success": success,
        "reason": reason,
        "recovery_started_at": timestamp,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def build_escalation_event(plan: RemediationPlan) -> dict:
    """Builds a recovery event for `planner.py`'s escalation branch —
    called only when `plan.remediation_required and plan.escalation_required`
    (see `planner.py`'s own docstring). Always
    `status=ESCALATION_REQUIRED, success=False, automatic=False` — an
    escalated fault is never reported as successfully recovered.
    """
    return _build_base_item(
        node_id=plan.node_id,
        timestamp=plan.timestamp,
        subsystem=plan.subsystem,
        fault_type=plan.fault_type,
        severity=plan.severity,
        action=plan.action.action_type.value,
        automatic=plan.automatic,
        status=RecoveryStatus.ESCALATION_REQUIRED.value,
        success=False,
        reason=plan.reason,
    )


def build_execution_event(plan: RemediationPlan, result: RemediationResult) -> Optional[dict]:
    """Builds a recovery event for `executor.py`'s outcome — called for
    every plan it receives. Returns `None` for `ExecutionStatus.SKIPPED`
    (nothing was required — nothing worth recording), matching Step 28's
    own metric-emission gating for that status exactly.

    Status mapping (mirrors `executor.py`'s own Step 28 metric-emission
    branches): `SUCCESS` -> `RecoveryStatus.SUCCESS`; `REJECTED` (an
    unsupported action — a mechanical execution failure, not a policy
    escalation) -> `RecoveryStatus.FAILURE`; `ESCALATED` (defensive here
    — see `executor.py`'s own docstring for why the real ASL-driven
    workflow never reaches this branch) -> `RecoveryStatus.ESCALATION_REQUIRED`.
    `success`/`fault_type`/`node_id`/`timestamp`/`action` come directly
    from `result` (the authoritative outcome); `severity`/`automatic`
    come from `plan` (`RemediationResult` carries no `severity` field of
    its own).
    """
    if result.status == ExecutionStatus.SUCCESS:
        status = RecoveryStatus.SUCCESS.value
    elif result.status == ExecutionStatus.ESCALATED:
        status = RecoveryStatus.ESCALATION_REQUIRED.value
    elif result.status == ExecutionStatus.REJECTED:
        status = RecoveryStatus.FAILURE.value
    else:  # ExecutionStatus.SKIPPED — nothing required, nothing to record
        return None

    return _build_base_item(
        node_id=result.node_id,
        timestamp=result.timestamp,
        subsystem=result.subsystem,
        fault_type=result.fault_type,
        severity=plan.severity,
        action=result.action.value,
        automatic=plan.automatic,
        status=status,
        success=result.success,
        reason=result.message,
    )


class RecoveryEventStore:
    """Writes recovery-event items to the recovery-events DynamoDB table.

    Accepts an optional pre-built DynamoDB Table resource (`table=`) for
    dependency injection — tests pass a fake/mock here, so no real AWS
    call is ever made during the test suite, mirroring
    `AnomalyEventStore`'s own `table=` injection pattern exactly.

    `table_name` resolution order: an explicit constructor argument, else
    the `RECOVERY_EVENTS_TABLE_NAME` environment variable, else
    `DEFAULT_TABLE_NAME` — so the deployed Lambda's environment
    configuration (see infrastructure/recovery-events-storage.yaml /
    infrastructure/remediation-lambdas.yaml) is the source of truth
    without hardcoding the table name in more than this one place.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        table: Optional[Any] = None,
    ):
        self.table_name = table_name or os.environ.get(RECOVERY_EVENTS_TABLE_NAME_ENV_VAR, DEFAULT_TABLE_NAME)
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

    def put_event(self, item: dict) -> PersistenceOutcome:
        """Writes one recovery-event item (already built by
        `build_escalation_event`/`build_execution_event`).

        Idempotent by design — see module docstring "Idempotency" for the
        full duplicate-vs-conflict reasoning. Raises
        `RecoveryEventConflictError` (a `RecoveryEventPersistenceError`
        subclass) for a genuine conflict, or plain
        `RecoveryEventPersistenceError` for any other DynamoDB failure.
        Callers are expected to let both propagate — see module docstring
        "Failure semantics".
        """
        node_id = item.get(_PARTITION_KEY)
        timestamp = item.get(_SORT_KEY)
        if not isinstance(node_id, str) or not node_id:
            raise InvalidRecoveryEventError(f"recovery event item is missing a usable {_PARTITION_KEY!r}: {node_id!r}")
        if not isinstance(timestamp, str) or not timestamp:
            raise InvalidRecoveryEventError(f"recovery event item is missing a usable {_SORT_KEY!r}: {timestamp!r}")

        table = self._get_table()
        dynamodb_item = _to_dynamodb_value(item)

        try:
            table.put_item(
                Item=dynamodb_item,
                ConditionExpression="attribute_not_exists(#pk) AND attribute_not_exists(#sk)",
                ExpressionAttributeNames={"#pk": _PARTITION_KEY, "#sk": _SORT_KEY},
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code == "ConditionalCheckFailedException":
                return self._resolve_duplicate(table, node_id, timestamp, item)
            raise RecoveryEventPersistenceError(
                f"Failed to persist recovery event for node '{node_id}' at '{timestamp}' "
                f"to table '{self.table_name}'."
            ) from exc
        except BotoCoreError as exc:
            raise RecoveryEventPersistenceError(
                f"Failed to persist recovery event for node '{node_id}' at '{timestamp}' "
                f"to table '{self.table_name}'."
            ) from exc

        return PersistenceOutcome.NEWLY_PERSISTED

    def _resolve_duplicate(self, table, node_id: str, timestamp: str, new_item: dict) -> PersistenceOutcome:
        """A PutItem already found an existing item for this key — reads
        it back and decides idempotent-retry vs. genuine conflict. See
        module docstring "Idempotency".
        """
        try:
            response = table.get_item(
                Key={_PARTITION_KEY: node_id, _SORT_KEY: timestamp},
                ConsistentRead=True,
            )
        except (ClientError, BotoCoreError) as exc:
            raise RecoveryEventPersistenceError(
                f"Recovery event for node '{node_id}' at '{timestamp}' already exists, but reading it "
                f"back to check for a conflict failed against table '{self.table_name}'."
            ) from exc

        existing = response.get("Item")
        if existing is None:
            # A ConditionalCheckFailedException means the item existed at
            # PutItem time; nothing in this system ever deletes recovery
            # events, so this should not happen. Fail loudly rather than
            # guess.
            raise RecoveryEventPersistenceError(
                f"Conditional check failed for node '{node_id}' at '{timestamp}', but no existing item "
                f"was found on read-back in table '{self.table_name}'."
            )

        conflicting_fields = [
            field for field in _CONFLICT_COMPARISON_FIELDS if existing.get(field) != new_item.get(field)
        ]
        if conflicting_fields:
            logger.error(
                "SentinelGrid recovery event CONFLICT for node_id=%s timestamp=%s: fields differ %s "
                "(existing=%s, new=%s)",
                node_id,
                timestamp,
                conflicting_fields,
                {f: existing.get(f) for f in conflicting_fields},
                {f: new_item.get(f) for f in conflicting_fields},
            )
            raise RecoveryEventConflictError(
                f"Recovery event for node '{node_id}' at '{timestamp}' already exists with conflicting "
                f"outcome data in field(s) {conflicting_fields}; refusing to overwrite historical evidence."
            )

        logger.info(
            "SentinelGrid recovery event already exists for node_id=%s timestamp=%s; "
            "skipping duplicate write (idempotent).",
            node_id,
            timestamp,
        )
        return PersistenceOutcome.DUPLICATE_ALREADY_EXISTS
