"""Unit tests for the local anomaly-detection -> remediation integration
layer (remediation/integration.py).

Everything here is local and deterministic: no AWS, no DynamoDB, no MQTT,
no simulator, no Lambda handler/event_store. Telemetry payloads use the
same shape as `tests/test_anomaly_detector.py`'s `_telemetry()` helper so
this test exercises the real `detect_telemetry()` classification rules,
not a re-implementation of them.

Tests 6 and 7 (HIGH/CRITICAL severity NODE_OVERLOAD) are the one exception:
the current detector only ever produces NODE_OVERLOAD at MEDIUM severity
(see detector.py's NUMERIC_FIELD_SPECS/`_check_numeric_field` — there is
only one "overload" band per field, with no HIGH/CRITICAL overload
classification). Per this task's explicit instruction not to modify
detector thresholds/classification just to manufacture a test case, those
two tests patch `detect_telemetry` (imported into `remediation.integration`)
to return a directly-constructed `DetectionResult` instead. This tests the
same integration boundary (DetectionResult -> RemediationEngine ->
RemediationExecutor, wired together by `process_telemetry()`) without
inventing or altering any detector behavior.
"""

import json
from unittest.mock import patch

from lambda_functions.anomaly_detector.detector import DetectionResult, FaultType, Severity
from remediation import ActionType, ExecutionStatus, RemediationIntegrationResult, process_telemetry


def _telemetry(subsystem, data, node_id="node-001", timestamp="2026-01-01T00:00:00+00:00", status="ONLINE"):
    return {
        "node_id": node_id,
        "subsystem": subsystem,
        "timestamp": timestamp,
        "status": status,
        "data": data,
    }


# --- 1. Normal telemetry ---


def test_normal_power_telemetry_requires_no_remediation():
    telemetry = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
    result = process_telemetry(telemetry)

    assert result.detection_result.detected is False
    assert result.remediation_plan.remediation_required is False
    assert result.remediation_result is None


# --- 2. SENSOR_DROPOUT ---


def test_power_sensor_dropout_restarts_sensor_automatically():
    telemetry = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0}, status="OFFLINE")
    result = process_telemetry(telemetry)

    assert result.detection_result.detected is True
    assert result.detection_result.fault_type == FaultType.SENSOR_DROPOUT.value

    assert result.remediation_plan.remediation_required is True
    assert result.remediation_plan.action.action_type == ActionType.RESTART_SENSOR
    assert result.remediation_plan.automatic is True

    assert result.remediation_result is not None
    assert result.remediation_result.success is True
    assert result.remediation_result.status == ExecutionStatus.SUCCESS
    assert result.remediation_result.action == ActionType.RESTART_SENSOR


# --- 3. NETWORK_PARTITION ---


def test_power_network_partition_reconnects_node_automatically():
    telemetry = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0})
    telemetry["network_status"] = "PARTITIONED"  # see detector.py's forward-looking hook
    result = process_telemetry(telemetry)

    assert result.detection_result.detected is True
    assert result.detection_result.fault_type == FaultType.NETWORK_PARTITION.value

    assert result.remediation_plan.remediation_required is True
    assert result.remediation_plan.action.action_type == ActionType.RECONNECT_NODE
    assert result.remediation_plan.automatic is True

    assert result.remediation_result is not None
    assert result.remediation_result.success is True
    assert result.remediation_result.status == ExecutionStatus.SUCCESS
    assert result.remediation_result.action == ActionType.RECONNECT_NODE


# --- 4. DATA_CORRUPTION ---


def test_power_data_corruption_resets_sensor_state_automatically():
    # voltage=999.0 is a finite number, but physically impossible for
    # power.voltage's normal range (200.0-250.0) -> DATA_CORRUPTION.
    telemetry = _telemetry("power", {"load": 50.0, "voltage": 999.0, "frequency": 50.0})
    result = process_telemetry(telemetry)

    assert result.detection_result.detected is True
    assert result.detection_result.fault_type == FaultType.DATA_CORRUPTION.value

    assert result.remediation_plan.remediation_required is True
    assert result.remediation_plan.action.action_type == ActionType.RESET_SENSOR_STATE
    assert result.remediation_plan.automatic is True

    assert result.remediation_result is not None
    assert result.remediation_result.success is True
    assert result.remediation_result.status == ExecutionStatus.SUCCESS
    assert result.remediation_result.action == ActionType.RESET_SENSOR_STATE


# --- 5. NODE_OVERLOAD / MEDIUM ---


def test_power_medium_overload_reduces_load_automatically():
    # power.load's overload band is (95.0, 100.0) -> NODE_OVERLOAD/MEDIUM.
    telemetry = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})
    result = process_telemetry(telemetry)

    assert result.detection_result.detected is True
    assert result.detection_result.fault_type == FaultType.NODE_OVERLOAD.value
    assert result.detection_result.severity == Severity.MEDIUM.value

    assert result.remediation_plan.remediation_required is True
    assert result.remediation_plan.action.action_type == ActionType.REDUCE_LOAD
    assert result.remediation_plan.automatic is True

    assert result.remediation_result is not None
    assert result.remediation_result.success is True
    assert result.remediation_result.status == ExecutionStatus.SUCCESS
    assert result.remediation_result.action == ActionType.REDUCE_LOAD


# --- 6 & 7. HIGH/CRITICAL severity NODE_OVERLOAD: escalate, never fake success ---
#
# See this file's module docstring: the current detector cannot produce
# NODE_OVERLOAD above MEDIUM severity from raw telemetry, so these two
# tests patch detect_telemetry (as imported into remediation.integration)
# to return a directly-constructed DetectionResult, purely to exercise the
# integration boundary's escalation handling.


def _overload_detection(severity: str) -> DetectionResult:
    return DetectionResult(
        detected=True,
        fault_type=FaultType.NODE_OVERLOAD.value,
        severity=severity,
        node_id="power-007",
        subsystem="power",
        timestamp="2026-01-01T00:05:00+00:00",
        reason=f"Field 'load' value 100.0 is within the overload band (95.0, 100.0), severity escalated to {severity} for this test.",
        evidence={"field": "load", "value": 100.0, "overload_range": (95.0, 100.0)},
    )


def test_high_severity_overload_requires_escalation_not_fake_success():
    with patch("remediation.integration.detect_telemetry", return_value=_overload_detection(Severity.HIGH.value)):
        result = process_telemetry(_telemetry("power", {"load": 100.0, "voltage": 230.0, "frequency": 50.0}))

    assert result.detection_result.severity == Severity.HIGH.value
    assert result.remediation_plan.remediation_required is True
    assert result.remediation_plan.automatic is False
    assert result.remediation_plan.escalation_required is True

    assert result.remediation_result is not None
    assert result.remediation_result.success is False
    assert result.remediation_result.status == ExecutionStatus.ESCALATED


def test_critical_severity_overload_requires_escalation_not_fake_success():
    with patch(
        "remediation.integration.detect_telemetry", return_value=_overload_detection(Severity.CRITICAL.value)
    ):
        result = process_telemetry(_telemetry("power", {"load": 100.0, "voltage": 230.0, "frequency": 50.0}))

    assert result.detection_result.severity == Severity.CRITICAL.value
    assert result.remediation_plan.remediation_required is True
    assert result.remediation_plan.automatic is False
    assert result.remediation_plan.escalation_required is True

    assert result.remediation_result is not None
    assert result.remediation_result.success is False
    assert result.remediation_result.status == ExecutionStatus.ESCALATED


# --- 8. INVALID_INPUT ---


def test_invalid_input_rejects_without_faking_repair():
    telemetry = _telemetry("unsupported_subsystem", {"load": 50.0})
    result = process_telemetry(telemetry)

    assert result.detection_result.detected is True
    assert result.detection_result.fault_type == FaultType.INVALID_INPUT.value

    assert result.remediation_plan.remediation_required is True
    assert result.remediation_plan.action.action_type == ActionType.REJECT_INPUT
    assert result.remediation_plan.automatic is False

    assert result.remediation_result is not None
    assert result.remediation_result.success is False
    assert result.remediation_result.status == ExecutionStatus.ESCALATED
    assert "succeeded" not in result.remediation_result.message.lower()


# --- 9. Unknown/unsupported fault result ---


def test_unknown_fault_type_yields_no_action_and_no_execution():
    unknown_detection = DetectionResult(
        detected=True,
        fault_type="SOME_FUTURE_FAULT_TYPE",
        severity=Severity.HIGH.value,
        node_id="power-007",
        subsystem="power",
        timestamp="2026-01-01T00:05:00+00:00",
        reason="test",
        evidence={},
    )
    with patch("remediation.integration.detect_telemetry", return_value=unknown_detection):
        result = process_telemetry(_telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0}))

    assert result.remediation_plan.remediation_required is False
    assert result.remediation_plan.action.action_type == ActionType.NO_ACTION
    assert result.remediation_result is None


# --- 10. End-to-end structure ---


def test_integration_result_contains_consistent_detection_plan_and_result():
    telemetry = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})
    result = process_telemetry(telemetry)

    assert isinstance(result, RemediationIntegrationResult)

    # Plan must correspond exactly to the detection result.
    assert result.remediation_plan.node_id == result.detection_result.node_id
    assert result.remediation_plan.subsystem == result.detection_result.subsystem
    assert result.remediation_plan.fault_type == result.detection_result.fault_type
    assert result.remediation_plan.severity == result.detection_result.severity
    assert result.remediation_plan.timestamp == result.detection_result.timestamp

    # Result must correspond exactly to the selected plan.
    assert result.remediation_result.node_id == result.remediation_plan.node_id
    assert result.remediation_result.subsystem == result.remediation_plan.subsystem
    assert result.remediation_result.fault_type == result.remediation_plan.fault_type
    assert result.remediation_result.action == result.remediation_plan.action.action_type
    assert result.remediation_result.timestamp == result.remediation_plan.timestamp


# --- 11. Determinism ---


def test_processing_the_same_telemetry_twice_is_deterministic():
    telemetry = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})

    result_a = process_telemetry(telemetry)
    result_b = process_telemetry(dict(telemetry))  # a fresh, equal-but-not-identical dict

    assert result_a == result_b


def test_processing_is_deterministic_across_representative_inputs():
    telemetries = [
        _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0}),
        _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0}, status="OFFLINE"),
        _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0}),
        _telemetry("unsupported_subsystem", {"load": 50.0}),
    ]
    for telemetry in telemetries:
        result_a = process_telemetry(dict(telemetry))
        result_b = process_telemetry(dict(telemetry))
        assert result_a == result_b, telemetry


# --- 12. Serialization ---


def test_integration_result_to_dict_is_json_serializable_for_normal_telemetry():
    telemetry = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
    result = process_telemetry(telemetry)

    payload = result.to_dict()
    json.dumps(payload)  # must not raise

    assert payload["detection_result"]["detected"] is False
    assert payload["remediation_plan"]["remediation_required"] is False
    assert payload["remediation_result"] is None


def test_integration_result_to_dict_is_json_serializable_for_anomalous_telemetry():
    telemetry = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})
    result = process_telemetry(telemetry)

    payload = result.to_dict()
    json.dumps(payload)  # must not raise

    assert payload["detection_result"]["detected"] is True
    assert payload["remediation_plan"]["remediation_required"] is True
    assert payload["remediation_result"]["status"] == ExecutionStatus.SUCCESS.value


# --- 13. AWS independence ---


def test_integration_module_has_no_aws_imports():
    import ast
    import pathlib

    module_path = pathlib.Path(__file__).resolve().parent.parent / "remediation" / "integration.py"
    forbidden = {"boto3", "botocore"}

    tree = ast.parse(module_path.read_text(), filename=str(module_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            continue
        assert not (names & forbidden), f"integration.py imports forbidden module(s): {names & forbidden}"
