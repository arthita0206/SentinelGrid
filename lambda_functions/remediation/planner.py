"""Thin AWS-Lambda-compatible adapter around RemediationEngine.plan().

    DetectionResult -> lambda_handler -> RemediationEngine.plan() -> RemediationPlan (only)

This module contains NO fault-to-action mapping of its own — it only
extracts a DetectionResult-shaped dict from the Lambda `event`, hands it
to the existing `remediation.engine.RemediationEngine.plan()`, and
returns the serialized `RemediationPlan` alone. All remediation policy
lives in `remediation/engine.py` and stays completely AWS-independent;
this file is the only AWS-facing piece, and even it makes no AWS SDK
calls — it just adapts a function call shape, mirroring
`lambda_functions/anomaly_detector/handler.py`'s own relationship to
`detector.py`.

The output is deliberately just the `RemediationPlan` — not wrapped with
`detection_result` — to match how
step_functions/remediation_workflow.json's `PlanRemediation` Task already
uses `ResultPath: "$.remediation_plan"`: Step Functions itself merges
this Lambda's raw return value into the workflow's accumulated state: it
owns state accumulation, this Lambda does not. See this package's README
"Step Functions contract" section for the full accumulation shape.

**Metrics (Step 28, FR-7.1)**: this handler owns `UnresolvedFaultCount`
for the escalation decision made HERE, at planning time — i.e. when
`RemediationEngine.plan()` decides `escalation_required=True` for a fault
that does need remediation (`remediation_required=True`,
`automatic=False`). This is the majority of this project's escalation
cases (HIGH/CRITICAL `NODE_OVERLOAD`, `INVALID_INPUT`, an unknown fault
type — see `remediation/engine.py`), and the workflow's own
`CheckAutomaticRemediation` Choice state routes straight to `Escalate`
for these WITHOUT ever invoking `executor.py` — so this is the only
place such an event's escalation can be observed at all. See
`lambda_functions/remediation/verifier.py` for the other
(mutually-exclusive, non-overlapping) `UnresolvedFaultCount` case: a plan
this module marked `automatic=True` whose remediation later failed
verification.

**Recovery history (Step 33)**: this handler also owns persisting an
`ESCALATION_REQUIRED` recovery-event record — via
`recovery_event_store.build_escalation_event()` +
`RecoveryEventStore.put_event()` — for the exact same escalation branch
that publishes `UnresolvedFaultCount` above; see
`lambda_functions/remediation/recovery_event_store.py`'s module docstring
for the full ownership/idempotency/failure-semantics reasoning, including
why a persistence failure here is allowed to fail this Task outright
(`PlanRemediation` has no `Catch` block in the existing, unmodified ASL —
a deliberate trade-off, not an oversight).

**Current node state (Step 34, FR-6.1)**: this handler also updates
`SentinelGridNodeState` — `RECOVERING` when it decides `automatic=True`
(an automatic remediation attempt is about to begin), `ESCALATION_REQUIRED`
for the same escalation branch as above. Keyed by the ORIGINATING
detection's own timestamp (`plan.timestamp`, unchanged from
`detection_result.timestamp`) — never a wall-clock time — so
`shared/node_state_store.py`'s ordering protection correctly rejects this
write if a genuinely newer telemetry event has already superseded it
(see that module's own docstring). Best-effort, unlike the fail-fast
recovery-event write above — a missed state update is not irreplaceable
historical evidence.
"""

from typing import Any

from lambda_functions.anomaly_detector.detector import DetectionResult
from remediation.engine import RemediationEngine
from shared.metrics import MetricName, MetricsClient
from shared.node_state_store import NodeState, NodeStateStore

from .recovery_event_store import RecoveryEventStore, build_escalation_event

# Same lazy-construction reasoning as lambda_functions/anomaly_detector/
# handler.py's own _metrics_client. Publication is best-effort — see
# shared/metrics.py.
_metrics_client = MetricsClient()

# Same lazy-construction reasoning as _metrics_client above. Unlike
# metrics, persistence here is fail-fast, not best-effort — see
# recovery_event_store.py's own module docstring "Failure semantics".
_recovery_event_store = RecoveryEventStore()

# Same lazy-construction reasoning as _metrics_client above. Best-effort
# — see shared/node_state_store.py's own module docstring.
_node_state_store = NodeStateStore()

_REQUIRED_FIELDS = ("detected", "fault_type", "severity", "node_id", "subsystem", "timestamp", "reason")
_OPTIONAL_STR_FIELDS = ("fault_type", "node_id", "subsystem", "timestamp")


class InvalidDetectionResultError(ValueError):
    """Raised when a Lambda event cannot be interpreted as a DetectionResult."""


def _extract_detection_result(event: Any) -> DetectionResult:
    """Validates and reconstructs a DetectionResult from a plain dict.

    Deliberately strict: every required field must be present with the
    correct basic type. Nothing missing is silently filled in — an
    incomplete or malformed event raises `InvalidDetectionResultError`
    instead, which `lambda_handler` turns into a structured error
    response (see this module's docstring / README "Error handling").
    """
    if not isinstance(event, dict):
        raise InvalidDetectionResultError(f"event must be a dict, got {type(event).__name__}")

    missing = [f for f in _REQUIRED_FIELDS if f not in event]
    if missing:
        raise InvalidDetectionResultError(f"event is missing required field(s): {missing}")

    if not isinstance(event["detected"], bool):
        raise InvalidDetectionResultError(f"'detected' must be a bool, got {type(event['detected']).__name__}")
    if not isinstance(event["severity"], str):
        raise InvalidDetectionResultError(f"'severity' must be a str, got {type(event['severity']).__name__}")
    if not isinstance(event["reason"], str):
        raise InvalidDetectionResultError(f"'reason' must be a str, got {type(event['reason']).__name__}")

    for field_name in _OPTIONAL_STR_FIELDS:
        value = event[field_name]
        if value is not None and not isinstance(value, str):
            raise InvalidDetectionResultError(
                f"'{field_name}' must be a str or None, got {type(value).__name__}"
            )

    evidence = event.get("evidence", {})
    if not isinstance(evidence, dict):
        raise InvalidDetectionResultError(f"'evidence' must be a dict, got {type(evidence).__name__}")

    return DetectionResult(
        detected=event["detected"],
        fault_type=event["fault_type"],
        severity=event["severity"],
        node_id=event["node_id"],
        subsystem=event["subsystem"],
        timestamp=event["timestamp"],
        reason=event["reason"],
        evidence=evidence,
    )


def lambda_handler(event: Any, context: Any = None) -> dict:
    """AWS Lambda entry point for the remediation planner.

    Input: a DetectionResult-shaped dict. Output on success: the
    serialized `RemediationPlan` (`RemediationPlan.to_dict()`) — and
    ONLY that, with no `detection_result`/`remediation_plan` wrapper.
    This matches step_functions/remediation_workflow.json's
    `PlanRemediation` state exactly: that Task's `ResultPath` is
    `$.remediation_plan`, which merges this Lambda's raw return value
    into the workflow's accumulated state at that key — Step Functions
    itself is what combines this plan with the original
    `detection_result`, not this Lambda. See README "Step Functions
    contract" for the full accumulation shape.

    On malformed input, returns a structured
    `{"error": "invalid_detection_result", "message": "..."}` response
    rather than raising — see README "Error handling" for why this
    strategy was chosen over letting an exception propagate.

    Never mutates `event`, never performs I/O, never uses randomness or
    wall-clock time, and calls nothing beyond
    `remediation.engine.RemediationEngine.plan()`.
    """
    try:
        detection_result = _extract_detection_result(event)
    except InvalidDetectionResultError as exc:
        return {"error": "invalid_detection_result", "message": str(exc)}

    plan = RemediationEngine().plan(detection_result)

    if plan.remediation_required and plan.escalation_required:
        _metrics_client.put_count(MetricName.UNRESOLVED_FAULT_COUNT, plan.subsystem, plan.fault_type)
        _recovery_event_store.put_event(build_escalation_event(plan))
        _node_state_store.update_state(
            node_id=plan.node_id,
            event_timestamp=plan.timestamp,
            state=NodeState.ESCALATION_REQUIRED.value,
            subsystem=plan.subsystem,
            fault_type=plan.fault_type,
            fault_severity=plan.severity,
            reason=plan.reason,
        )
    elif plan.remediation_required and plan.automatic:
        _node_state_store.update_state(
            node_id=plan.node_id,
            event_timestamp=plan.timestamp,
            state=NodeState.RECOVERING.value,
            subsystem=plan.subsystem,
            fault_type=plan.fault_type,
            fault_severity=plan.severity,
            reason=plan.reason,
        )

    return plan.to_dict()
