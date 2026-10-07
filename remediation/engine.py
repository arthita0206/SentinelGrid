"""Deterministic local remediation engine and executor.

    DetectionResult -> RemediationEngine.plan() -> RemediationPlan
                                                        |
                                                        v
                                    RemediationExecutor.execute() -> RemediationResult

`RemediationEngine` decides *what* should be done about a `DetectionResult`
(see `lambda_functions/anomaly_detector/detector.py`); `RemediationExecutor`
carries out that decision as a **safe, local simulation** — it never
changes real node state, network configuration, or any external system.
Both are plain functions of their input: no randomness, no clock, no I/O,
no AWS SDK. See this package's README for the full fault -> action mapping
and safety policy.

This module imports `DetectionResult`/`FaultType`/`Severity` from the
anomaly detector purely for their field/value definitions (matching
`fault_type`/`severity` strings) — it does not import or depend on
`handler.py` or `event_store.py`, so it carries no AWS dependency at all.
"""

from typing import Optional

from lambda_functions.anomaly_detector.detector import DetectionResult, FaultType, Severity

from .models import ActionType, ExecutionStatus, RemediationAction, RemediationPlan, RemediationResult

# Fault types for which the mapped action is always automatic, regardless
# of severity. NODE_OVERLOAD is deliberately excluded — its automatic-ness
# depends on severity (see RemediationEngine.plan()) — and INVALID_INPUT
# is deliberately excluded — it is never automatic (see README).
_ALWAYS_AUTOMATIC_ACTIONS = {
    FaultType.SENSOR_DROPOUT.value: (
        ActionType.RESTART_SENSOR,
        "Simulate restarting/resetting the affected sensor so telemetry can resume.",
    ),
    FaultType.NETWORK_PARTITION.value: (
        ActionType.RECONNECT_NODE,
        "Simulate reconnecting the affected node/network connection.",
    ),
    FaultType.DATA_CORRUPTION.value: (
        ActionType.RESET_SENSOR_STATE,
        "Simulate resetting the affected sensor's state/calibration so clean telemetry can resume.",
    ),
}

# Severities for which a NODE_OVERLOAD is permitted to remediate
# automatically. Anything else (HIGH, CRITICAL) requires escalation — see
# this package's README for why this project does not invent a more
# aggressive automatic action for those severities.
_NODE_OVERLOAD_AUTOMATIC_SEVERITIES = {Severity.MEDIUM.value}


def _no_action_plan(
    reason: str,
    node_id: Optional[str] = None,
    subsystem: Optional[str] = None,
    fault_type: Optional[str] = None,
    severity: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> RemediationPlan:
    """A plan that recommends no remediation action at all — used for
    detected=False, unknown fault types, and malformed/missing input.
    """
    return RemediationPlan(
        remediation_required=False,
        node_id=node_id,
        subsystem=subsystem,
        fault_type=fault_type,
        severity=severity,
        timestamp=timestamp,
        action=RemediationAction(action_type=ActionType.NO_ACTION, description=reason, parameters={}),
        reason=reason,
        automatic=False,
        escalation_required=False,
    )


def _non_empty_str(value) -> bool:
    return isinstance(value, str) and bool(value)


class RemediationEngine:
    """Maps a `DetectionResult` to a `RemediationPlan`, deterministically.

    Never guesses: a fault type outside the fixed mapping in this module
    (including one this project's detector doesn't currently produce)
    always yields a no-action plan rather than an invented remediation.
    """

    def plan(self, detection: Optional[DetectionResult]) -> RemediationPlan:
        if detection is None:
            return _no_action_plan("No DetectionResult was provided; nothing to remediate.")

        if not detection.detected:
            return _no_action_plan(
                "No fault was detected; no remediation required.",
                node_id=detection.node_id,
                subsystem=detection.subsystem,
                fault_type=detection.fault_type,
                severity=detection.severity,
                timestamp=detection.timestamp,
            )

        node_id = detection.node_id
        subsystem = detection.subsystem
        fault_type = detection.fault_type
        severity = detection.severity

        if not _non_empty_str(fault_type):
            return _no_action_plan(
                "DetectionResult is missing a valid fault_type; refusing to plan remediation on incomplete data.",
                node_id=node_id if _non_empty_str(node_id) else None,
                subsystem=subsystem if _non_empty_str(subsystem) else None,
                severity=severity,
                timestamp=detection.timestamp,
            )

        # INVALID_INPUT is checked before the node_id/subsystem completeness
        # gate below on purpose: the detector produces INVALID_INPUT
        # *precisely* in cases where node_id/subsystem may be missing or
        # unparseable (see detector.py's _invalid_event_result). REJECT_INPUT
        # doesn't target a specific node's state, so it doesn't need those
        # fields to be a safe, well-defined action.
        if fault_type == FaultType.INVALID_INPUT.value:
            return RemediationPlan(
                remediation_required=True,
                node_id=node_id if _non_empty_str(node_id) else None,
                subsystem=subsystem if _non_empty_str(subsystem) else None,
                fault_type=fault_type,
                severity=severity,
                timestamp=detection.timestamp,
                action=RemediationAction(
                    action_type=ActionType.REJECT_INPUT,
                    description="Reject the invalid telemetry rather than attempting to repair unknown/unsafe data.",
                    parameters=dict(detection.evidence),
                ),
                reason=f"Telemetry was classified as INVALID_INPUT: {detection.reason}",
                automatic=False,
                escalation_required=True,
            )

        if not (_non_empty_str(node_id) and _non_empty_str(subsystem)):
            return _no_action_plan(
                "DetectionResult is missing required field(s) (node_id/subsystem); "
                "refusing to plan remediation on incomplete data.",
                node_id=node_id if _non_empty_str(node_id) else None,
                subsystem=subsystem if _non_empty_str(subsystem) else None,
                fault_type=fault_type,
                severity=severity,
                timestamp=detection.timestamp,
            )

        if fault_type == FaultType.NODE_OVERLOAD.value:
            automatic = severity in _NODE_OVERLOAD_AUTOMATIC_SEVERITIES
            return RemediationPlan(
                remediation_required=True,
                node_id=node_id,
                subsystem=subsystem,
                fault_type=fault_type,
                severity=severity,
                timestamp=detection.timestamp,
                action=RemediationAction(
                    action_type=ActionType.REDUCE_LOAD,
                    description="Simulate reducing load on the affected node.",
                    parameters=dict(detection.evidence),
                ),
                reason=(
                    f"NODE_OVERLOAD at severity {severity}: {detection.reason}"
                    if automatic
                    else (
                        f"NODE_OVERLOAD at severity {severity} exceeds the automatic-remediation policy "
                        f"(only {sorted(_NODE_OVERLOAD_AUTOMATIC_SEVERITIES)} is automatic); "
                        "escalating for manual intervention rather than inventing a more aggressive action."
                    )
                ),
                automatic=automatic,
                escalation_required=not automatic,
            )

        if fault_type in _ALWAYS_AUTOMATIC_ACTIONS:
            action_type, description = _ALWAYS_AUTOMATIC_ACTIONS[fault_type]
            return RemediationPlan(
                remediation_required=True,
                node_id=node_id,
                subsystem=subsystem,
                fault_type=fault_type,
                severity=severity,
                timestamp=detection.timestamp,
                action=RemediationAction(
                    action_type=action_type,
                    description=description,
                    parameters=dict(detection.evidence),
                ),
                reason=f"{fault_type} detected: {detection.reason}",
                automatic=True,
                escalation_required=False,
            )

        # Unknown/unsupported fault type: never guess.
        return _no_action_plan(
            f"Fault type {fault_type!r} has no defined remediation mapping; no action is taken.",
            node_id=node_id,
            subsystem=subsystem,
            fault_type=fault_type,
            severity=severity,
            timestamp=detection.timestamp,
        )


class RemediationExecutor:
    """Executes a `RemediationPlan` as a safe, local, deterministic
    simulation. Never touches real infrastructure, network state, or any
    external system — see this package's README for the full rationale.
    """

    def execute(self, plan: RemediationPlan) -> RemediationResult:
        if not isinstance(plan.action.action_type, ActionType) or plan.action.action_type not in ActionType:
            return RemediationResult(
                success=False,
                status=ExecutionStatus.REJECTED,
                node_id=plan.node_id,
                subsystem=plan.subsystem,
                fault_type=plan.fault_type,
                action=ActionType.NO_ACTION,
                timestamp=plan.timestamp,
                message=f"Unsupported/unknown action {plan.action.action_type!r}; refusing to execute.",
                simulated=True,
                details={},
            )

        if not plan.remediation_required or plan.action.action_type == ActionType.NO_ACTION:
            return RemediationResult(
                success=False,
                status=ExecutionStatus.SKIPPED,
                node_id=plan.node_id,
                subsystem=plan.subsystem,
                fault_type=plan.fault_type,
                action=ActionType.NO_ACTION,
                timestamp=plan.timestamp,
                message=plan.reason,
                simulated=True,
                details={},
            )

        if not plan.automatic or plan.escalation_required:
            return RemediationResult(
                success=False,
                status=ExecutionStatus.ESCALATED,
                node_id=plan.node_id,
                subsystem=plan.subsystem,
                fault_type=plan.fault_type,
                action=plan.action.action_type,
                timestamp=plan.timestamp,
                message=(
                    f"Action {plan.action.action_type.value} requires manual review/escalation; "
                    "it was NOT automatically executed, simulated or otherwise."
                ),
                simulated=True,
                details={"parameters": plan.action.parameters},
            )

        return RemediationResult(
            success=True,
            status=ExecutionStatus.SUCCESS,
            node_id=plan.node_id,
            subsystem=plan.subsystem,
            fault_type=plan.fault_type,
            action=plan.action.action_type,
            timestamp=plan.timestamp,
            message=f"Simulated execution of {plan.action.action_type.value} succeeded.",
            simulated=True,
            details={"parameters": plan.action.parameters},
        )
