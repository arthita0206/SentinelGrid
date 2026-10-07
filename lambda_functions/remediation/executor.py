"""Thin AWS-Lambda-compatible adapter around RemediationExecutor.execute().

    {detection_result, remediation_plan} -> lambda_handler -> RemediationExecutor.execute()
        -> RemediationResult (only)

This module contains NO execution/escalation policy of its own — it only
extracts a RemediationPlan-shaped dict from the accumulated Step
Functions state, hands it to the existing
`remediation.engine.RemediationExecutor.execute()`, and returns the
serialized `RemediationResult` alone. `RemediationExecutor` remains
exactly what it already was: a safe, local, deterministic SIMULATION —
this adapter does not turn it into a real infrastructure actuator, and
makes no AWS SDK calls of its own.

The output is deliberately just the `RemediationResult` — not wrapped
with `detection_result`/`remediation_plan` — to match how
step_functions/remediation_workflow.json's `ExecuteRemediation` Task
already uses `ResultPath: "$.remediation_result"`: Step Functions itself
merges this Lambda's raw return value into the workflow's accumulated
state, so this Lambda does not need to (and must not) re-carry
`detection_result`/`remediation_plan` through its own output — that
accumulation is Step Functions' responsibility, not this adapter's. See
this package's README "Step Functions contract" section.

**Metrics (Step 28, FR-7.1)**: this handler owns `RecoveryAttemptCount`,
`RecoverySuccessCount`, `RecoveryFailureCount`, and `RecoveryLatency`.
`ExecuteRemediation` is only ever reached, per
`step_functions/remediation_workflow.json`'s own `CheckAutomaticRemediation`
Choice state, for a plan with `automatic=True` — so every valid plan
this handler receives already represents a genuine, actual remediation
attempt (`RecoveryEngine.plan()` guarantees `escalation_required=True`
implies `automatic=False`, so a plan reaching here always has
`escalation_required=False` too). `RecoveryAttemptCount` is therefore
published once a valid plan is extracted, before calling `execute()`.
The subsequent `ExecutionStatus` then maps to exactly one of:
`SUCCESS` -> `RecoverySuccessCount` + `RecoveryLatency` (measured from
`detection_result.timestamp`, already present in the workflow's
accumulated input — see Step 28's "do not modify the ASL" constraint);
`REJECTED` (an unsupported `action_type` — defensive, not currently
reachable from a real `RemediationEngine.plan()` output) ->
`RecoveryFailureCount`; `ESCALATED` (also defensive here — see
`planner.py`'s own docstring for why the real workflow's escalation path
never reaches this Lambda at all) -> `RecoveryFailureCount` AND
`UnresolvedFaultCount`; `SKIPPED` -> no metric (nothing was required).
Publication is best-effort (see `shared/metrics.py`) and never affects
`RemediationExecutor.execute()`'s own return value.

**Recovery history (Step 33)**: this handler also owns persisting a
recovery-event record for every plan it receives (`SUCCESS`/`FAILURE`/
`ESCALATION_REQUIRED`, mapped from the same `ExecutionStatus` branches as
the metrics above — see `recovery_event_store.build_execution_event()`'s
own docstring for the exact mapping; returns `None`, and nothing is
persisted, for `SKIPPED`). Unlike Planner's persistence, a failure here
is allowed to propagate and fail this Task — `ExecuteRemediation` already
has a `Catch` routing any failure to `Escalate` in the existing,
unmodified ASL, so this reuses an already-designed resilience path
rather than needing a new one. See
`lambda_functions/remediation/recovery_event_store.py`'s module docstring
for the full reasoning.

**Current node state (Step 34, FR-6.1)**: this handler also updates
`SentinelGridNodeState`, mapped from the same `ExecutionStatus` branches:
`SUCCESS` -> `RECOVERED`; `REJECTED` -> `FAULT_DETECTED` (a mechanical
execution failure — the fault is still present, not escalated by
policy); `ESCALATED` (defensive) -> `ESCALATION_REQUIRED`; `SKIPPED` ->
no write. Keyed by `result.timestamp` (== the originating detection
timestamp, unchanged through the workflow) for the same ordering
protection reasoning as `planner.py`'s own state updates. Best-effort —
see `shared/node_state_store.py`.

**Note on `RECOVERED` and verification**: `RemediationEngine`'s current,
deterministic simulation guarantees a `SUCCESS` `RemediationResult`'s
`action`/`node_id`/`subsystem`/`fault_type` are always copied directly
from the plan that produced it, so `verifier.py`'s own consistency check
cannot currently fail for a genuine `SUCCESS` reached via the real,
unmodified ASL — writing `RECOVERED` here rather than deferring to
`verifier.py` is therefore safe today. `verifier.py` still holds a
`dynamodb:UpdateItem` permission of its own, and writes
`ESCALATION_REQUIRED` if it ever observes `verified=False` — a defensive
backstop against exactly this becoming untrue in a future change, so
"do not falsely mark an escalated fault as RECOVERED" holds unconditionally,
not just "in the current architecture."

**Not deployed.** This is a local, Lambda-shaped handler only.
"""

from typing import Any

from remediation.engine import RemediationExecutor
from remediation.models import ActionType, ExecutionStatus, RemediationAction, RemediationPlan
from shared.metrics import MetricName, MetricsClient, compute_elapsed_ms
from shared.node_state_store import NodeState, NodeStateStore

from .recovery_event_store import RecoveryEventStore, build_execution_event

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

_REQUIRED_PLAN_FIELDS = ("remediation_required", "automatic", "action")


class InvalidRemediationPlanError(ValueError):
    """Raised when a Lambda event cannot be interpreted as a RemediationPlan."""


def _extract_remediation_plan(event: Any) -> RemediationPlan:
    """Validates and reconstructs a RemediationPlan from the planner's
    output shape (`{"remediation_plan": {...}, ...}`).

    Deliberately strict about structural completeness (a missing
    `remediation_required`/`automatic`/`action` is a malformed plan, not
    something to default), but deliberately LENIENT about an
    *unrecognized* `action_type` value: rather than rejecting it here,
    that raw value is passed through unchanged into the reconstructed
    `RemediationPlan`, and `RemediationExecutor.execute()`'s own existing
    defensive check (see remediation/engine.py) is left to reject it via
    `ExecutionStatus.REJECTED`. Re-validating `action_type` against
    `ActionType` here would duplicate remediation policy this adapter is
    explicitly not supposed to own.
    """
    if not isinstance(event, dict):
        raise InvalidRemediationPlanError(f"event must be a dict, got {type(event).__name__}")

    plan_dict = event.get("remediation_plan")
    if not isinstance(plan_dict, dict):
        raise InvalidRemediationPlanError("event is missing a valid 'remediation_plan' object")

    missing = [f for f in _REQUIRED_PLAN_FIELDS if f not in plan_dict]
    if missing:
        raise InvalidRemediationPlanError(f"remediation_plan is missing required field(s): {missing}")

    if not isinstance(plan_dict["remediation_required"], bool):
        raise InvalidRemediationPlanError("remediation_plan.remediation_required must be a bool")
    if not isinstance(plan_dict["automatic"], bool):
        raise InvalidRemediationPlanError("remediation_plan.automatic must be a bool")

    action_dict = plan_dict["action"]
    if not isinstance(action_dict, dict) or "action_type" not in action_dict:
        raise InvalidRemediationPlanError("remediation_plan.action is missing or invalid")

    raw_action_type = action_dict["action_type"]
    try:
        action_type = ActionType(raw_action_type)
    except ValueError:
        action_type = raw_action_type  # see docstring: left for RemediationExecutor to reject safely

    action = RemediationAction(
        action_type=action_type,
        description=action_dict.get("description", ""),
        parameters=action_dict.get("parameters", {}),
    )

    return RemediationPlan(
        remediation_required=plan_dict["remediation_required"],
        node_id=plan_dict.get("node_id"),
        subsystem=plan_dict.get("subsystem"),
        fault_type=plan_dict.get("fault_type"),
        severity=plan_dict.get("severity"),
        timestamp=plan_dict.get("timestamp"),
        action=action,
        reason=plan_dict.get("reason", ""),
        automatic=plan_dict["automatic"],
        escalation_required=plan_dict.get("escalation_required", False),
    )


def lambda_handler(event: Any, context: Any = None) -> dict:
    """AWS Lambda entry point for the remediation executor.

    Input: `{"detection_result": {...}, "remediation_plan": {...}}` (the
    Step-Functions-accumulated state after `PlanRemediation`). Output on
    success: the serialized `RemediationResult`
    (`RemediationResult.to_dict()`) — and ONLY that, with no
    `detection_result`/`remediation_plan` carried through. This is always
    safe to serialize via `.to_dict()`: `RemediationExecutor.execute()`
    guarantees its returned `action` is always a real `ActionType` member
    in every branch, including the REJECTED one (even though the INPUT
    plan's `action_type` may have been an unrecognized raw value — see
    `_extract_remediation_plan`).

    On malformed input, returns a structured
    `{"error": "invalid_remediation_plan", "message": "..."}` response
    rather than raising. Escalated/failed remediation is never turned
    into a false success — this handler only forwards whatever
    `RemediationExecutor.execute()` actually decided.

    Never mutates `event`, never performs I/O, never uses randomness or
    wall-clock time, and never actuates anything real.
    """
    try:
        plan = _extract_remediation_plan(event)
    except InvalidRemediationPlanError as exc:
        return {"error": "invalid_remediation_plan", "message": str(exc)}

    is_attempt = plan.remediation_required and plan.automatic and not plan.escalation_required
    if is_attempt:
        _metrics_client.put_count(MetricName.RECOVERY_ATTEMPT_COUNT, plan.subsystem, plan.fault_type)

    result = RemediationExecutor().execute(plan)

    recovery_event = build_execution_event(plan, result)
    if recovery_event is not None:
        _recovery_event_store.put_event(recovery_event)

    if result.status == ExecutionStatus.SUCCESS:
        _metrics_client.put_count(MetricName.RECOVERY_SUCCESS_COUNT, result.subsystem, result.fault_type)
        detection_result = event.get("detection_result") if isinstance(event, dict) else None
        detection_timestamp = detection_result.get("timestamp") if isinstance(detection_result, dict) else None
        latency_ms = compute_elapsed_ms(detection_timestamp)
        if latency_ms is not None:
            _metrics_client.put_milliseconds(MetricName.RECOVERY_LATENCY, result.subsystem, result.fault_type, latency_ms)
        _node_state_store.update_state(
            node_id=result.node_id,
            event_timestamp=result.timestamp,
            state=NodeState.RECOVERED.value,
            subsystem=result.subsystem,
            fault_type=result.fault_type,
            fault_severity=plan.severity,
            reason=result.message,
        )
    elif result.status in (ExecutionStatus.REJECTED, ExecutionStatus.ESCALATED):
        _metrics_client.put_count(MetricName.RECOVERY_FAILURE_COUNT, result.subsystem, result.fault_type)
        if result.status == ExecutionStatus.ESCALATED:
            _metrics_client.put_count(MetricName.UNRESOLVED_FAULT_COUNT, result.subsystem, result.fault_type)
            _node_state_store.update_state(
                node_id=result.node_id,
                event_timestamp=result.timestamp,
                state=NodeState.ESCALATION_REQUIRED.value,
                subsystem=result.subsystem,
                fault_type=result.fault_type,
                fault_severity=plan.severity,
                reason=result.message,
            )
        else:
            _node_state_store.update_state(
                node_id=result.node_id,
                event_timestamp=result.timestamp,
                state=NodeState.FAULT_DETECTED.value,
                subsystem=result.subsystem,
                fault_type=result.fault_type,
                fault_severity=plan.severity,
                reason=result.message,
            )

    return result.to_dict()
