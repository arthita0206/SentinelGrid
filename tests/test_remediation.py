"""Unit tests for the local remediation engine (remediation/).

Everything here is local and deterministic: no AWS, no DynamoDB, no
simulator, no MQTT. `DetectionResult` instances are constructed directly
(not via detect_telemetry()) so each fault/severity combination — including
ones the current detector doesn't actually produce yet, like a HIGH/CRITICAL
NODE_OVERLOAD — can be exercised explicitly against the remediation policy.
"""

import copy

from lambda_functions.anomaly_detector.detector import DetectionResult, FaultType, Severity
from remediation import (
    ActionType,
    ExecutionStatus,
    RemediationAction,
    RemediationEngine,
    RemediationExecutor,
    RemediationPlan,
    RemediationResult,
)


def _detection(**overrides) -> DetectionResult:
    defaults = dict(
        detected=True,
        fault_type=FaultType.NODE_OVERLOAD.value,
        severity=Severity.MEDIUM.value,
        node_id="power-007",
        subsystem="power",
        timestamp="2026-01-01T00:05:00+00:00",
        reason="Field 'load' value 97.8 is within the overload band (95.0, 100.0).",
        evidence={"field": "load", "value": 97.8, "overload_range": (95.0, 100.0)},
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


# --- 1. Normal telemetry: no remediation required ---


def test_normal_detection_requires_no_remediation():
    detection = _detection(detected=False, fault_type=None, severity=Severity.NORMAL.value, reason="ok", evidence={})
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is False
    assert plan.automatic is False
    assert plan.escalation_required is False
    assert plan.action.action_type == ActionType.NO_ACTION


# --- 2. SENSOR_DROPOUT ---


def test_sensor_dropout_plans_restart_sensor_automatically():
    detection = _detection(
        fault_type=FaultType.SENSOR_DROPOUT.value,
        severity=Severity.HIGH.value,
        reason="Telemetry status is 'OFFLINE' instead of 'ONLINE'.",
        evidence={"status": "OFFLINE"},
    )
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is True
    assert plan.action.action_type == ActionType.RESTART_SENSOR
    assert plan.automatic is True
    assert plan.escalation_required is False

    result = RemediationExecutor().execute(plan)
    assert result.success is True
    assert result.status == ExecutionStatus.SUCCESS
    assert result.simulated is True
    assert result.action == ActionType.RESTART_SENSOR


# --- 3. NETWORK_PARTITION ---


def test_network_partition_plans_reconnect_node_automatically():
    detection = _detection(
        fault_type=FaultType.NETWORK_PARTITION.value,
        severity=Severity.HIGH.value,
        reason="Telemetry payload's 'network_status' indicates the node is network-partitioned.",
        evidence={"network_status": "PARTITIONED"},
    )
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is True
    assert plan.action.action_type == ActionType.RECONNECT_NODE
    assert plan.automatic is True
    assert plan.escalation_required is False

    result = RemediationExecutor().execute(plan)
    assert result.success is True
    assert result.status == ExecutionStatus.SUCCESS
    assert result.action == ActionType.RECONNECT_NODE


# --- 4. DATA_CORRUPTION ---


def test_data_corruption_plans_reset_sensor_state_automatically():
    detection = _detection(
        fault_type=FaultType.DATA_CORRUPTION.value,
        severity=Severity.CRITICAL.value,
        reason="Field 'frequency' is not a finite number (nan).",
        evidence={"field": "frequency", "value": "nan"},
    )
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is True
    assert plan.action.action_type == ActionType.RESET_SENSOR_STATE
    assert plan.automatic is True
    assert plan.escalation_required is False

    result = RemediationExecutor().execute(plan)
    assert result.success is True
    assert result.status == ExecutionStatus.SUCCESS
    assert result.action == ActionType.RESET_SENSOR_STATE


def test_data_corruption_is_automatic_regardless_of_severity():
    # Only NODE_OVERLOAD is severity-gated per the defined policy; every
    # DATA_CORRUPTION severity the detector actually produces (LOW/HIGH/
    # CRITICAL) must remain automatic.
    for severity in (Severity.LOW.value, Severity.HIGH.value, Severity.CRITICAL.value):
        detection = _detection(fault_type=FaultType.DATA_CORRUPTION.value, severity=severity)
        plan = RemediationEngine().plan(detection)
        assert plan.automatic is True, f"severity={severity}"
        assert plan.escalation_required is False, f"severity={severity}"


# --- 5. NODE_OVERLOAD / MEDIUM ---


def test_node_overload_medium_plans_reduce_load_automatically():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is True
    assert plan.action.action_type == ActionType.REDUCE_LOAD
    assert plan.automatic is True
    assert plan.escalation_required is False

    result = RemediationExecutor().execute(plan)
    assert result.success is True
    assert result.status == ExecutionStatus.SUCCESS
    assert result.action == ActionType.REDUCE_LOAD


# --- 6 & 7. NODE_OVERLOAD / HIGH & CRITICAL: escalate, don't auto-act ---


def test_node_overload_high_requires_escalation():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is True
    assert plan.automatic is False
    assert plan.escalation_required is True

    result = RemediationExecutor().execute(plan)
    assert result.success is False
    assert result.status == ExecutionStatus.ESCALATED


def test_node_overload_critical_requires_escalation():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.CRITICAL.value)
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is True
    assert plan.automatic is False
    assert plan.escalation_required is True

    result = RemediationExecutor().execute(plan)
    assert result.success is False
    assert result.status == ExecutionStatus.ESCALATED


# --- 8. INVALID_INPUT: reject, never repair ---


def test_invalid_input_plans_reject_input_not_automatic():
    detection = _detection(
        fault_type=FaultType.INVALID_INPUT.value,
        severity=Severity.CRITICAL.value,
        node_id=None,
        subsystem=None,
        reason="Telemetry payload could not be parsed as JSON.",
        evidence={"raw_event_type": "str"},
    )
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is True
    assert plan.action.action_type == ActionType.REJECT_INPUT
    assert plan.automatic is False
    assert plan.escalation_required is True

    result = RemediationExecutor().execute(plan)
    assert result.success is False
    assert result.status == ExecutionStatus.ESCALATED
    assert result.action == ActionType.REJECT_INPUT
    # Must not claim the input was repaired/fixed.
    assert "succeeded" not in result.message.lower()


# --- 9. Unknown fault type: never guess ---


def test_unknown_fault_type_yields_no_action():
    detection = _detection(fault_type="SOME_FUTURE_FAULT_TYPE", severity=Severity.HIGH.value)
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is False
    assert plan.action.action_type == ActionType.NO_ACTION
    assert plan.automatic is False

    result = RemediationExecutor().execute(plan)
    assert result.success is False
    assert result.status == ExecutionStatus.SKIPPED


# --- 10. Missing required DetectionResult fields: safe failure ---


def test_missing_node_id_yields_safe_no_action_plan():
    detection = _detection(node_id=None)
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is False
    assert plan.action.action_type == ActionType.NO_ACTION


def test_missing_subsystem_yields_safe_no_action_plan():
    detection = _detection(subsystem=None)
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is False
    assert plan.action.action_type == ActionType.NO_ACTION


def test_missing_fault_type_yields_safe_no_action_plan():
    detection = _detection(fault_type=None)
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is False
    assert plan.action.action_type == ActionType.NO_ACTION


def test_none_detection_result_yields_safe_no_action_plan():
    plan = RemediationEngine().plan(None)

    assert plan.remediation_required is False
    assert plan.action.action_type == ActionType.NO_ACTION


# --- 11. detected == False: no remediation ---


def test_detected_false_yields_no_remediation_even_with_fault_type_set():
    # Defensive: even if fault_type/severity were somehow populated
    # alongside detected=False, remediation must not be planned.
    detection = _detection(detected=False, fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = RemediationEngine().plan(detection)

    assert plan.remediation_required is False
    assert plan.action.action_type == ActionType.NO_ACTION


# --- 12. Determinism ---


def test_planning_is_deterministic_for_the_same_detection_result():
    detection = _detection()
    plan_a = RemediationEngine().plan(detection)
    plan_b = RemediationEngine().plan(copy.deepcopy(detection))

    assert plan_a == plan_b


def test_planning_is_deterministic_across_all_fault_types():
    for fault_type, severity in [
        (FaultType.SENSOR_DROPOUT.value, Severity.HIGH.value),
        (FaultType.NETWORK_PARTITION.value, Severity.HIGH.value),
        (FaultType.DATA_CORRUPTION.value, Severity.CRITICAL.value),
        (FaultType.NODE_OVERLOAD.value, Severity.MEDIUM.value),
        (FaultType.NODE_OVERLOAD.value, Severity.HIGH.value),
        (FaultType.INVALID_INPUT.value, Severity.CRITICAL.value),
    ]:
        detection = _detection(fault_type=fault_type, severity=severity)
        plan_a = RemediationEngine().plan(detection)
        plan_b = RemediationEngine().plan(detection)
        assert plan_a == plan_b, f"{fault_type}/{severity}"


# --- 13. Serialization ---


def test_remediation_plan_to_dict_is_json_serializable():
    import json

    detection = _detection()
    plan = RemediationEngine().plan(detection)

    payload = plan.to_dict()
    json.dumps(payload)  # must not raise

    assert payload["remediation_required"] is True
    assert payload["action"]["action_type"] == ActionType.REDUCE_LOAD.value
    assert isinstance(payload["action"], dict)


def test_remediation_result_to_dict_is_json_serializable():
    import json

    detection = _detection()
    plan = RemediationEngine().plan(detection)
    result = RemediationExecutor().execute(plan)

    payload = result.to_dict()
    json.dumps(payload)  # must not raise

    assert payload["status"] == ExecutionStatus.SUCCESS.value
    assert payload["action"] == ActionType.REDUCE_LOAD.value
    assert payload["simulated"] is True


# --- 14. Executor edge cases ---


def test_executor_returns_escalated_for_manual_action_without_false_success():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = RemediationEngine().plan(detection)
    result = RemediationExecutor().execute(plan)

    assert result.success is False
    assert result.status != ExecutionStatus.SUCCESS


def test_executor_rejects_unknown_action_type_safely():
    plan = RemediationPlan(
        remediation_required=True,
        node_id="power-007",
        subsystem="power",
        fault_type="NODE_OVERLOAD",
        severity="MEDIUM",
        timestamp="2026-01-01T00:05:00+00:00",
        action=RemediationAction(action_type="SOME_MADE_UP_ACTION", description="not a real action", parameters={}),
        reason="test",
        automatic=True,
    )
    result = RemediationExecutor().execute(plan)

    assert result.success is False
    assert result.status == ExecutionStatus.REJECTED


def test_executor_skips_when_no_remediation_required():
    detection = _detection(detected=False, fault_type=None, severity=Severity.NORMAL.value)
    plan = RemediationEngine().plan(detection)
    result = RemediationExecutor().execute(plan)

    assert result.success is False
    assert result.status == ExecutionStatus.SKIPPED


# --- 15. No AWS dependency ---


def test_remediation_package_has_no_aws_imports():
    import ast
    import pathlib

    package_dir = pathlib.Path(__file__).resolve().parent.parent / "remediation"
    forbidden = {"boto3", "botocore"}

    for py_file in package_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not (names & forbidden), f"{py_file.name} imports forbidden module(s): {names & forbidden}"
