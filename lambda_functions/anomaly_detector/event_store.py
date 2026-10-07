"""Persists anomalous DetectionResults to the SentinelGridAnomalyEvents
DynamoDB table.

Kept as its own module, separate from detector.py, so the detector stays
completely AWS-independent (see detector.py's own docstring) — this is
the one place in the anomaly_detector package that talks to DynamoDB,
mirroring how simulator/aws_publisher.py is the one place in the
simulator package that talks to AWS IoT Core.

The table itself is defined in
infrastructure/anomaly-events-storage.yaml — not yet deployed as of this
writing, and this Lambda's execution role does not yet have
dynamodb:PutItem on it either. Nothing here makes that assumption; a real
write against an undeployed table or a role without permission would
simply raise `AnomalyEventPersistenceError`, exactly like any other
failure this module can produce (see `AnomalyEventStore.put_event`).
"""

import decimal
import logging
import math
from enum import Enum
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .detector import DetectionResult

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # see handler.py for why this is necessary on Lambda

DEFAULT_TABLE_NAME = "SentinelGridAnomalyEvents"

# The DynamoDB item's partition/sort key attribute names, matching
# infrastructure/anomaly-events-storage.yaml's KeySchema exactly.
_PARTITION_KEY = "node_id"
_SORT_KEY = "timestamp"


class PersistenceOutcome(str, Enum):
    """What `AnomalyEventStore.put_event()` actually did — both are
    successful outcomes; this exists purely so callers (the Lambda
    handler) can log accurately, without changing persistence semantics
    at all. See `put_event`'s docstring for the full reasoning."""

    NEWLY_PERSISTED = "NEWLY_PERSISTED"
    DUPLICATE_ALREADY_EXISTS = "DUPLICATE_ALREADY_EXISTS"


class AnomalyEventPersistenceError(RuntimeError):
    """Raised when an anomaly event could not be written to DynamoDB."""


def _to_dynamodb_value(value: Any) -> Any:
    """Recursively converts a plain Python value into one boto3's DynamoDB
    *resource* interface (as opposed to the low-level client) accepts.

    Two real gotchas this exists to handle:
      - boto3's resource-level serializer rejects plain `float` values
        outright (`TypeError: Float types are not supported. Use Decimal
        types instead.`) — every float must become a `decimal.Decimal`.
      - DynamoDB's Number type has no representation for NaN or
        +/-Infinity at all. A `DetectionResult` can legitimately contain
        one of these in `evidence` (a DATA_CORRUPTION detection triggered
        by a NaN/Infinite telemetry reading) — those are converted to
        their string form instead, so the record can still be written and
        still faithfully shows what was detected.
      - A tuple (e.g. `evidence["overload_range"]`, a `(min, max)` pair
        from detector.py's field specs) is converted to a list, matching
        how it would already look after a JSON round-trip.
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


def _build_item(result: DetectionResult) -> dict:
    """Maps a DetectionResult 1:1 onto an anomaly-event item.

    No separate event_id field is added: `node_id` + `timestamp` is
    already this table's primary key and is a sufficient, natural
    identity for one detection on one node at one moment — nothing yet
    needs to cross-reference these records by a separate id. Adding one
    now would be an unused field; it can be added later if a real need
    (e.g. correlating with a future Step Functions execution) arises.
    """
    item = {
        _PARTITION_KEY: result.node_id,
        _SORT_KEY: result.timestamp,
        "subsystem": result.subsystem,
        "fault_type": result.fault_type,
        "severity": result.severity,
        "reason": result.reason,
        "evidence": result.evidence,
        "detected": result.detected,
    }
    return _to_dynamodb_value(item)


class AnomalyEventStore:
    """Writes anomalous DetectionResults to the anomaly-events DynamoDB table.

    Accepts an optional pre-built DynamoDB Table resource (`table=`) for
    dependency injection — tests pass a fake/mock here, so no real AWS
    call is ever made during the test suite. Production code (see
    handler.py) lets it build its own client lazily on first use, exactly
    like `AwsIotPublisher` does for its IoT Data client: constructing an
    `AnomalyEventStore()` never touches AWS by itself.

    No `profile` is used when this runs as the deployed Lambda — unlike
    `AwsIotPublisher` (used by the *local simulator*, which needs an
    explicit AWS CLI profile since it runs on a developer machine), this
    class runs *inside* Lambda, where ambient credentials come from the
    function's own execution role. `region`/`profile` are still accepted
    as optional constructor arguments for flexibility (e.g. a future local
    testing script), but `handler.py`'s module-level instance passes
    neither.
    """

    def __init__(
        self,
        table_name: str = DEFAULT_TABLE_NAME,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        table: Optional[Any] = None,
    ):
        self.table_name = table_name
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

    def put_event(self, result: DetectionResult) -> PersistenceOutcome:
        """Writes one anomaly event.

        This method always writes — it has no opinion about *when* it
        should be called. `handler.py` decides that (only for
        `detected=True` results that have a usable `node_id`/`timestamp`);
        this method's only job is turning one `DetectionResult` into one
        correctly-typed DynamoDB item.

        Idempotent by design: the write is conditioned on the item not
        already existing (`attribute_not_exists` on both key attributes).
        Since the primary key is exactly (node_id, timestamp), a retried
        Lambda invocation for the same detection produces the same key —
        the conditional write makes that a silent no-op rather than a
        duplicate or an overwrite of unrelated concurrent data. A
        `ConditionalCheckFailedException` is therefore NOT treated as an
        error here — this idempotency behavior, and the condition
        expression itself, are unchanged by the return value below.

        Returns `PersistenceOutcome.NEWLY_PERSISTED` if this call actually
        wrote the item, or `PersistenceOutcome.DUPLICATE_ALREADY_EXISTS` if
        the item already existed and the write was skipped. Both are
        successful outcomes — the distinction exists purely so callers can
        log accurately (a duplicate skip is not a fresh persist, and
        logging it as one would be misleading), not to change behavior.

        Raises `AnomalyEventPersistenceError` for any other DynamoDB
        failure. Callers (the Lambda handler) are expected to let this
        propagate — this module never silently swallows a real failure,
        per this project's fail-fast persistence policy (see README).
        """
        item = _build_item(result)
        table = self._get_table()

        try:
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#pk) AND attribute_not_exists(#sk)",
                ExpressionAttributeNames={"#pk": _PARTITION_KEY, "#sk": _SORT_KEY},
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code == "ConditionalCheckFailedException":
                logger.info(
                    "SentinelGrid anomaly event already exists for node_id=%s timestamp=%s; "
                    "skipping duplicate write (idempotent).",
                    result.node_id,
                    result.timestamp,
                )
                return PersistenceOutcome.DUPLICATE_ALREADY_EXISTS
            raise AnomalyEventPersistenceError(
                f"Failed to persist anomaly event for node '{result.node_id}' "
                f"at '{result.timestamp}' to table '{self.table_name}'."
            ) from exc
        except BotoCoreError as exc:
            raise AnomalyEventPersistenceError(
                f"Failed to persist anomaly event for node '{result.node_id}' "
                f"at '{result.timestamp}' to table '{self.table_name}'."
            ) from exc

        return PersistenceOutcome.NEWLY_PERSISTED
