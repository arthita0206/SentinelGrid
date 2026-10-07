"""Thin AWS Lambda adapter around the local anomaly detector.

    AWS IoT event -> lambda_handler -> extract payload -> detect_telemetry()
        -> serialize DetectionResult -> Lambda response
                                              |
                                    (if anomalous) persist to DynamoDB
                                              |
                                    (only if persistence succeeded)
                                    submit async remediation trigger invocation

This module contains NO detection rules of its own — it only extracts a
telemetry-shaped payload from the Lambda `event` and hands it to
`detect_telemetry()` (see detector.py), then serializes the resulting
`DetectionResult`. All classification logic lives in detector.py and
stays completely AWS-independent. This file itself has no direct AWS SDK
import — it delegates all AWS calls to `event_store.py` (DynamoDB),
`remediation_trigger_client.py` (Lambda invoke), and (Step 28)
`shared/metrics.py` (CloudWatch custom metrics), the dedicated modules
that own those integrations, exactly mirroring each other's design.

**Metrics (Step 28, FR-7.1)**: this handler owns `FaultDetectionCount`
and `DetectionLatency` — published once per invocation where
`detected=True` and a usable `subsystem`/`fault_type` identity is
present (see `_publish_detection_metrics`). Publication is best-effort
(see `shared/metrics.py`): a CloudWatch failure is logged and otherwise
ignored, and never affects detection, persistence, or remediation-trigger
submission below.

**Current node state (Step 34, FR-6.1)**: this handler also owns
updating `SentinelGridNodeState` — `HEALTHY` for `detected=False`,
`FAULT_DETECTED` for `detected=True` — for every result with a usable
`node_id`/`timestamp` (see `_update_node_state`). Like metrics, this is
best-effort (see `shared/node_state_store.py`'s module docstring for why
current state specifically is NOT fail-fast, unlike anomaly persistence
below) and never affects detection, persistence, or remediation-trigger
submission.

**Deployed**: this handler runs as the `SentinelGridAnomalyDetector`
Lambda function, invoked by the `sentinelgrid_telemetry_to_lambda` IoT
Rule. See this package's README for supported event shapes and how to
invoke it locally.

Because IoT invokes Lambda asynchronously, the return value below is
never visible to the MQTT publisher or anything else in AWS — the only
way to observe a result operationally is the structured log line this
handler emits (see `_log_result`).
"""

import json
import logging
from typing import Any

from shared.metrics import MetricName, MetricsClient, compute_elapsed_ms
from shared.node_state_store import NodeState, NodeStateStore

from .detector import DetectionResult, FaultType, Severity, detect_telemetry
from .event_store import AnomalyEventPersistenceError, AnomalyEventStore, PersistenceOutcome
from .remediation_trigger_client import RemediationTriggerClient, RemediationTriggerSubmissionError

logger = logging.getLogger(__name__)
# AWS Lambda's Python runtime attaches a handler to the root logger, but
# leaves its level at Python's own default (WARNING). A named logger with
# no level of its own (NOTSET) inherits that WARNING threshold, so
# logger.info(...) below would be silently dropped before it ever reaches
# that handler. Setting this logger's own level to INFO is enough to fix
# that — it doesn't touch the root logger or call basicConfig, so it has
# no effect on any other module's logging.
logger.setLevel(logging.INFO)

# Built once per execution environment (Lambda reuses this across warm
# invocations) rather than per-call. Constructing it does not touch AWS —
# it only connects lazily on the first actual put_event() call. No
# `profile` is passed: inside a deployed Lambda, ambient credentials come
# from the function's own execution role, not a local CLI profile.
_event_store = AnomalyEventStore()

# Same lazy-construction reasoning as _event_store above. Reads the
# target Lambda's ARN from the REMEDIATION_TRIGGER_FUNCTION_ARN
# environment variable once, at construction time — see
# remediation_trigger_client.py.
_trigger_client = RemediationTriggerClient()

# Same lazy-construction reasoning as _event_store above (Step 28). See
# shared/metrics.py: constructing this never touches AWS, and every
# publish call it makes is best-effort — see _publish_detection_metrics.
_metrics_client = MetricsClient()

# Same lazy-construction reasoning as _event_store above (Step 34). See
# shared/node_state_store.py: constructing this never touches AWS, and
# every update call it makes is best-effort — see _update_node_state.
_node_state_store = NodeStateStore()


def _extract_payload(event: Any) -> Any:
    """Best-effort extraction of a telemetry-shaped object from `event`.

    Supported shapes:
      1. `event` is already the telemetry dict — the expected shape when an
         AWS IoT Rule's Lambda action invokes this function with
         `SELECT *`: IoT Core passes the selected message as the event
         itself, with no extra envelope.
      2. `event` is a JSON string encoding that same dict (e.g. a manually
         pasted Lambda console test event).

    Anything else — including a dict that isn't shaped like telemetry — is
    returned unchanged: `detect_telemetry()` already handles non-dict or
    malformed input by returning an `INVALID_INPUT` result, so this
    function doesn't need its own fallback classification for that case.
    A no-envelope design is used deliberately: IoT Rule Lambda actions
    don't wrap the payload, so supporting an artificial envelope (e.g. a
    `{"body": ...}` shape borrowed from API Gateway) would add complexity
    this trigger type doesn't need.
    """
    if isinstance(event, str):
        return json.loads(event)  # may raise json.JSONDecodeError
    return event


def _invalid_event_result(event: Any, error: Exception) -> DetectionResult:
    return DetectionResult(
        detected=True,
        fault_type=FaultType.INVALID_INPUT.value,
        severity=Severity.CRITICAL.value,
        node_id=None,
        subsystem=None,
        timestamp=None,
        reason=f"Lambda event could not be parsed as JSON: {error}",
        evidence={"raw_event_type": type(event).__name__},
    )


def _log_result(result: DetectionResult) -> None:
    """Structured, operational log of the detection result (INFO level).

    Safe to log: `DetectionResult` only ever contains telemetry-derived
    classification fields (detected/fault_type/severity/node_id/subsystem/
    timestamp/reason/evidence) — never credentials, environment variables,
    or the raw Lambda event. This is additional observability only; it
    does not affect what `lambda_handler` returns.
    """
    logger.info("SentinelGrid detection result: %s", json.dumps(result.to_dict(), sort_keys=True))


def _publish_detection_metrics(result: DetectionResult) -> None:
    """Publishes `FaultDetectionCount` and (when possible) `DetectionLatency`
    for one detection result (Step 28, FR-7.1). Runs unconditionally
    whenever `detected=True`, regardless of what persistence/remediation
    trigger submission later do with this result — detection itself
    already succeeded by the time this is called, which is the whole of
    what `FaultDetectionCount` measures. See `shared/metrics.py`'s module
    docstring for the "Subsystem"/"FaultType" dimension contract:
    `MetricsClient` already skips publication (logging why) when either
    value is missing, so this function does not need its own guard for
    that — e.g. an `INVALID_INPUT` result with no recoverable `subsystem`
    naturally publishes nothing, without special-casing that fault type
    here.

    `DetectionLatency` is computed as `now - result.timestamp` (the
    telemetry event's own timestamp, not a CloudWatch ingestion time —
    see `shared.metrics.compute_elapsed_ms`), and is only published when
    that calculation succeeds; a malformed or future timestamp already
    causes `compute_elapsed_ms` to log and return `None` rather than
    raise, so one bad telemetry event can never crash detection here
    either.

    Never raises: both publish calls below are already best-effort at
    the `MetricsClient` layer (a CloudWatch failure is logged and
    swallowed there), so this function has nothing further to catch.
    """
    if not result.detected:
        return

    _metrics_client.put_count(MetricName.FAULT_DETECTION_COUNT, result.subsystem, result.fault_type)

    latency_ms = compute_elapsed_ms(result.timestamp)
    if latency_ms is not None:
        _metrics_client.put_milliseconds(MetricName.DETECTION_LATENCY, result.subsystem, result.fault_type, latency_ms)


def _update_node_state(result: DetectionResult, telemetry_status: Any) -> None:
    """Updates the node's current-state row (Step 34, FR-6.1) —
    `HEALTHY` for `detected=False`, `FAULT_DETECTED` (carrying
    `fault_type`/`fault_severity`) for `detected=True`. Skipped entirely
    when `node_id`/`timestamp` aren't both usable — the same identity
    gate `_should_persist` uses below — since there is nowhere to key a
    state row without them (matches Step 34's "invalid input with no
    usable node_id: do not create state" requirement).

    `telemetry_status` is the raw telemetry payload's own `status` field
    (`"ONLINE"`/`"OFFLINE"`) — NOT part of `DetectionResult` itself, so
    it is passed through separately from whatever `lambda_handler`
    already extracted; `None` when unavailable (e.g. the payload never
    parsed as JSON at all), in which case the state row simply omits
    `status` rather than fabricating a value.

    Best-effort — see `shared/node_state_store.py`'s module docstring for
    why current state is deliberately NOT fail-fast, unlike anomaly
    persistence below. Never raises.
    """
    if not (result.node_id and result.timestamp):
        return

    if result.detected:
        state = NodeState.FAULT_DETECTED.value
        fault_type = result.fault_type
        fault_severity = result.severity
    else:
        state = NodeState.HEALTHY.value
        fault_type = None
        fault_severity = None

    status = telemetry_status if isinstance(telemetry_status, str) else None

    _node_state_store.update_state(
        node_id=result.node_id,
        event_timestamp=result.timestamp,
        state=state,
        status=status,
        subsystem=result.subsystem,
        fault_type=fault_type,
        fault_severity=fault_severity,
        reason=result.reason,
    )


def _should_persist(result: DetectionResult) -> bool:
    """An anomaly is persisted only when there is something to persist and
    somewhere to key it: `detected=True`, and both `node_id` and
    `timestamp` present (they are this table's partition/sort key, so a
    record cannot be written without them).

    In practice this means all four genuine fault classifications
    (SENSOR_DROPOUT, NETWORK_PARTITION, DATA_CORRUPTION, NODE_OVERLOAD)
    are always persisted, since detect_telemetry() only ever reaches those
    classifications after node_id/timestamp have already been validated
    as present. An INVALID_INPUT result is persisted only in the rare
    case both fields still happen to be present (e.g. an unsupported
    subsystem value on an otherwise well-formed message) — it is skipped
    whenever the identifying fields themselves are what's missing/invalid
    (e.g. a message that failed to parse as JSON at all), since there
    would be nothing valid to key the record on. See README for the fuller
    reasoning.
    """
    return bool(result.detected and result.node_id and result.timestamp)


def _persist_if_anomalous(result: DetectionResult) -> bool:
    """Writes an anomaly-event record for this result, if `_should_persist`.

    Persistence failures are NOT swallowed: they are logged, then
    re-raised, so AWS Lambda reports the invocation as failed rather than
    silently pretending the event was recorded. This is a deliberate
    fail-fast choice for this first implementation (see README) — it runs
    strictly after `_log_result`, so it never affects the detection-result
    log line or the `DetectionResult` that was already computed.

    Only a genuinely new write logs "anomaly event persisted" — a
    duplicate (idempotent, not an error) logs nothing further here, since
    `event_store.py` already logged its own "already exists ... skipping
    duplicate write" message for that case. Without this distinction, a
    retried invocation for the same event would misleadingly log both
    messages, as if it had both skipped and freshly persisted the same
    write.

    Returns `True` iff persistence was actually attempted and succeeded
    (either outcome — a fresh write or an idempotent duplicate-skip both
    count), and `False` if `_should_persist` was `False` (nothing to
    persist). This return value is what `lambda_handler` uses to decide
    whether to submit a remediation trigger — see `_submit_remediation_trigger`
    and Step 22's "trigger only after persistence succeeds" requirement.
    A persistence *failure* never returns `False`; it raises instead, so
    callers can't mistake "failed" for "nothing to do."
    """
    if not _should_persist(result):
        return False

    try:
        outcome = _event_store.put_event(result)
    except AnomalyEventPersistenceError:
        logger.exception(
            "SentinelGrid anomaly event persistence FAILED for node_id=%s timestamp=%s",
            result.node_id,
            result.timestamp,
        )
        raise
    else:
        if outcome == PersistenceOutcome.NEWLY_PERSISTED:
            logger.info(
                "SentinelGrid anomaly event persisted: node_id=%s timestamp=%s fault_type=%s severity=%s",
                result.node_id,
                result.timestamp,
                result.fault_type,
                result.severity,
            )
        return True


def _submit_remediation_trigger(result: DetectionResult) -> None:
    """Asynchronously hands off a persisted anomaly to the remediation
    trigger Lambda (see remediation_trigger_client.py). Only ever called
    after `_persist_if_anomalous(result)` has returned `True` — never for
    normal telemetry, never for a result that failed persistence (that
    already raised and stopped execution before this is reached).

    Submission failures are NOT swallowed: they are logged, then
    re-raised, matching `_persist_if_anomalous`'s own fail-fast policy —
    a failure to even submit the asynchronous invocation must not be
    silently reported as a successful detector invocation. This says
    nothing about whether the remediation trigger's own downstream
    processing succeeds afterward; that is intentionally outside this
    function's visibility (see remediation_trigger_client.py's module
    docstring).
    """
    try:
        _trigger_client.submit(result)
    except RemediationTriggerSubmissionError:
        logger.exception(
            "SentinelGrid remediation trigger submission FAILED for node_id=%s timestamp=%s",
            result.node_id,
            result.timestamp,
        )
        raise
    else:
        logger.info(
            "SentinelGrid remediation trigger submitted: node_id=%s timestamp=%s fault_type=%s severity=%s",
            result.node_id,
            result.timestamp,
            result.fault_type,
            result.severity,
        )


def lambda_handler(event: Any, context: Any = None) -> dict:
    """AWS Lambda entry point. Delegates all detection logic to detect_telemetry().

    Returns the JSON-serializable `DetectionResult.to_dict()` directly —
    there is no extra Lambda-response envelope (no `statusCode`/`body`),
    since an IoT Rule Lambda action doesn't need one. The same result is
    also logged at INFO (see `_log_result`) and, if anomalous, persisted
    to DynamoDB (see `_persist_if_anomalous`) before being returned.

    A persistence failure propagates as an exception out of this function
    — it is not caught here — so the return value in that case is never
    reached; AWS Lambda reports the invocation as failed instead. The
    same is true of a remediation-trigger submission failure (see
    `_submit_remediation_trigger`), which is only ever attempted after
    persistence has actually succeeded — never for normal telemetry,
    never when nothing was persisted.
    """
    try:
        payload = _extract_payload(event)
    except json.JSONDecodeError as exc:
        result = _invalid_event_result(event, exc)
        _log_result(result)
        _publish_detection_metrics(result)
        _update_node_state(result, None)
        if _persist_if_anomalous(result):
            _submit_remediation_trigger(result)
        return result.to_dict()

    result = detect_telemetry(payload)
    _log_result(result)
    _publish_detection_metrics(result)
    _update_node_state(result, payload.get("status") if isinstance(payload, dict) else None)
    if _persist_if_anomalous(result):
        _submit_remediation_trigger(result)
    return result.to_dict()
