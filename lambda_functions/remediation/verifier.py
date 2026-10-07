"""AWS-Lambda-compatible adapter that evaluates (never re-executes) a
RemediationResult.

    {detection_result, remediation_plan, remediation_result}
        -> lambda_handler
        -> {"verified": bool, "reason": str}   (only)

This module represents the future verification boundary described in
step_functions/remediation_workflow.json's `VerifyRemediation` state.
For this step, verification is a SAFE SIMULATION: it only inspects the
already-computed, already-simulated `RemediationResult` structure — it
never queries AWS, DynamoDB, IoT, a real sensor, or anything external,
and it never changes the `RemediationResult` it's given. There is no
real-world health check here, deliberately: this project is
software-only, and this Lambda's whole job right now is to prove the
verification *shape* the workflow needs, not to invent a real check.

The output is deliberately just the verification object — not wrapped
with `detection_result`/`remediation_plan`/`remediation_result` — to
match how step_functions/remediation_workflow.json's `VerifyRemediation`
Task already uses `ResultPath: "$.verification"`: Step Functions itself
merges this Lambda's raw return value into the workflow's accumulated
state. See this package's README "Step Functions contract" section.

**Metrics (Step 28, FR-7.1)**: this handler owns the OTHER
`UnresolvedFaultCount` case (see `planner.py`'s own docstring for the
first): a remediation that `executor.py` reported as `SUCCESS` but that
this Lambda's own `_verify()` determined did NOT actually verify —
`step_functions/remediation_workflow.json`'s `CheckVerification` Choice
state routes this straight to `Escalate`. Published once, here, only
when `verified is False` and a usable `subsystem`/`fault_type` identity
is available from `detection_result` — never duplicating `planner.py`'s
own publication, since a plan this module ever sees already had
`automatic=True` (otherwise `executor.py`, and therefore this Lambda,
would never have been reached at all — see `executor.py`'s docstring).
Publication is best-effort (see `shared/metrics.py`) and never affects
`_verify()`'s own return value.

**Current node state (Step 34, FR-6.1)**: for that same `verified=False`
case, this handler ALSO writes `SentinelGridNodeState` ->
`ESCALATION_REQUIRED` — a defensive backstop against `executor.py`
having already optimistically written `RECOVERED` for what turned out
not to actually verify (see `executor.py`'s own docstring "Note on
RECOVERED and verification" for why that optimism is safe under the
current, unmodified engine, and why this backstop exists anyway). Keyed
by `detection_result.timestamp`, matching every other remediation-driven
state write. Best-effort — see `shared/node_state_store.py`.

**Not deployed.** This is a local, Lambda-shaped handler only.
"""

from typing import Any, Optional

from shared.metrics import MetricName, MetricsClient
from shared.node_state_store import NodeState, NodeStateStore

# Same lazy-construction reasoning as lambda_functions/anomaly_detector/
# handler.py's own _metrics_client. Publication is best-effort — see
# shared/metrics.py.
_metrics_client = MetricsClient()

# Same lazy-construction reasoning as _metrics_client above. Best-effort
# — see shared/node_state_store.py's own module docstring.
_node_state_store = NodeStateStore()

_SUCCESS_STATUS = "SUCCESS"
_CORRESPONDENCE_FIELDS = ("node_id", "subsystem", "fault_type")


def _verify(remediation_plan: Optional[dict], remediation_result: Optional[dict]) -> dict:
    """Pure function: given (at most) a plan dict and a result dict,
    decides `verified`/`reason`. Never raises — every branch below
    returns a `{"verified": bool, "reason": str}` dict, even when the
    inputs are missing or malformed. Never mutates either argument.
    """
    if not isinstance(remediation_result, dict):
        return {
            "verified": False,
            "reason": "remediation_result is missing or not an object; verification cannot be performed.",
        }

    success = remediation_result.get("success")
    status = remediation_result.get("status")

    if success is not True or status != _SUCCESS_STATUS:
        return {
            "verified": False,
            "reason": (
                f"Remediation did not succeed (success={success!r}, status={status!r}); "
                "verification cannot succeed when the underlying remediation did not succeed."
            ),
        }

    if isinstance(remediation_plan, dict):
        plan_action_dict = remediation_plan.get("action")
        plan_action_type = plan_action_dict.get("action_type") if isinstance(plan_action_dict, dict) else None
        result_action_type = remediation_result.get("action")
        if plan_action_type is not None and result_action_type != plan_action_type:
            return {
                "verified": False,
                "reason": (
                    f"remediation_result.action ({result_action_type!r}) does not correspond to "
                    f"remediation_plan.action.action_type ({plan_action_type!r})."
                ),
            }

        for field_name in _CORRESPONDENCE_FIELDS:
            plan_value = remediation_plan.get(field_name)
            result_value = remediation_result.get(field_name)
            if plan_value is not None and result_value != plan_value:
                return {
                    "verified": False,
                    "reason": (
                        f"remediation_result.{field_name} ({result_value!r}) does not correspond to "
                        f"remediation_plan.{field_name} ({plan_value!r})."
                    ),
                }

    return {
        "verified": True,
        "reason": "Simulated remediation completed successfully and corresponds to the remediation plan.",
    }


def lambda_handler(event: Any, context: Any = None) -> dict:
    """AWS Lambda entry point for remediation verification.

    Input: `{"detection_result": {...}, "remediation_plan": {...},
    "remediation_result": {...}}` (the Step-Functions-accumulated state
    after `ExecuteRemediation`). Output: always exactly
    `{"verified": bool, "reason": str}` — never wrapped with
    `detection_result`/`remediation_plan`/`remediation_result`, and,
    unlike `planner.lambda_handler`/`executor.lambda_handler`, never a
    distinct `{"error": ...}` envelope on malformed input either. This is
    deliberate: `verified` is exactly the boolean
    step_functions/remediation_workflow.json's `CheckVerification` state
    branches on (via `$.verification.verified`, once Step Functions
    merges this return value in at `ResultPath: "$.verification"`), so
    this Lambda must always produce it, even from garbage input, rather
    than fail in a shape that state can't route on. See README "Error
    handling" for the full reasoning and how this differs from the other
    two handlers.

    Never fabricates success: `verified` is only ever `True` when the
    given `remediation_result` itself reports success AND corresponds to
    the given `remediation_plan`. Never mutates `event`, never performs
    I/O, never queries anything real, and never modifies
    `remediation_result` itself — it only reads it.
    """
    remediation_plan = event.get("remediation_plan") if isinstance(event, dict) else None
    remediation_result = event.get("remediation_result") if isinstance(event, dict) else None

    verification = _verify(remediation_plan, remediation_result)

    if verification["verified"] is False:
        detection_result = event.get("detection_result") if isinstance(event, dict) else None
        node_id = detection_result.get("node_id") if isinstance(detection_result, dict) else None
        subsystem = detection_result.get("subsystem") if isinstance(detection_result, dict) else None
        fault_type = detection_result.get("fault_type") if isinstance(detection_result, dict) else None
        severity = detection_result.get("severity") if isinstance(detection_result, dict) else None
        timestamp = detection_result.get("timestamp") if isinstance(detection_result, dict) else None
        _metrics_client.put_count(MetricName.UNRESOLVED_FAULT_COUNT, subsystem, fault_type)
        _node_state_store.update_state(
            node_id=node_id,
            event_timestamp=timestamp,
            state=NodeState.ESCALATION_REQUIRED.value,
            subsystem=subsystem,
            fault_type=fault_type,
            fault_severity=severity,
            reason=verification["reason"],
        )

    return verification
