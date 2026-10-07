"""Unit tests for the local anomaly/fault detector (lambda_functions/anomaly_detector).

The detector runs entirely on the payload it receives — these tests never
touch AWS, DynamoDB, or FaultInjectionEngine.
"""

import math

from lambda_functions.anomaly_detector import DetectionResult, FaultType, Severity, detect_telemetry


def _telemetry(subsystem, data, node_id="node-001", timestamp="2026-01-01T00:00:00+00:00", status="ONLINE"):
    return {
        "node_id": node_id,
        "subsystem": subsystem,
        "timestamp": timestamp,
        "status": status,
        "data": data,
    }


# --- NORMAL ---


def test_normal_power_telemetry():
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
    result = detect_telemetry(payload)

    assert result.detected is False
    assert result.fault_type is None
    assert result.severity == Severity.NORMAL.value
    assert result.node_id == "node-001"
    assert result.subsystem == "power"
    assert result.reason
    assert "data" in result.evidence


def test_normal_water_telemetry():
    payload = _telemetry("water", {"pressure": 4.0, "flow_rate": 50.0, "reservoir_level": 70.0})
    result = detect_telemetry(payload)

    assert result.detected is False
    assert result.fault_type is None
    assert result.severity == Severity.NORMAL.value


def test_normal_traffic_telemetry():
    payload = _telemetry("traffic", {"vehicle_count": 40, "average_speed": 45.0, "signal_state": "GREEN"})
    result = detect_telemetry(payload)

    assert result.detected is False
    assert result.fault_type is None
    assert result.severity == Severity.NORMAL.value


def test_normal_traffic_low_speed_is_not_overload():
    # A near-stopped node (heavy congestion) is plausible and normal on its
    # own — only vehicle_count exceeding its ceiling signals overload.
    payload = _telemetry("traffic", {"vehicle_count": 40, "average_speed": 1.5, "signal_state": "RED"})
    result = detect_telemetry(payload)

    assert result.detected is False
    assert result.severity == Severity.NORMAL.value


# --- SENSOR_DROPOUT ---


def test_sensor_dropout_via_non_online_status():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0}, status="OFFLINE")
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.SENSOR_DROPOUT.value
    assert result.severity == Severity.HIGH.value
    assert "OFFLINE" in result.reason


def test_sensor_dropout_via_missing_data_dict():
    payload = _telemetry("power", None)
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.SENSOR_DROPOUT.value
    assert result.severity == Severity.HIGH.value


def test_sensor_dropout_via_empty_data_dict():
    payload = _telemetry("water", {})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.SENSOR_DROPOUT.value


def test_sensor_dropout_via_partially_missing_required_fields():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0})  # frequency missing
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.SENSOR_DROPOUT.value
    assert result.evidence["missing_fields"] == ["frequency"]


# --- DATA_CORRUPTION ---


def test_data_corruption_impossible_voltage():
    payload = _telemetry("power", {"load": 50.0, "voltage": 650.0, "frequency": 50.0})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.DATA_CORRUPTION.value
    assert result.severity == Severity.HIGH.value
    assert result.evidence["field"] == "voltage"


def test_data_corruption_negative_pressure():
    payload = _telemetry("water", {"pressure": -5.0, "flow_rate": 50.0, "reservoir_level": 70.0})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.DATA_CORRUPTION.value
    assert result.severity == Severity.HIGH.value
    assert result.evidence["field"] == "pressure"


def test_data_corruption_impossible_reservoir_level():
    payload = _telemetry("water", {"pressure": 4.0, "flow_rate": 50.0, "reservoir_level": 250.0})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.DATA_CORRUPTION.value
    assert result.evidence["field"] == "reservoir_level"


def test_data_corruption_negative_vehicle_count():
    payload = _telemetry("traffic", {"vehicle_count": -10, "average_speed": 40.0, "signal_state": "RED"})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.DATA_CORRUPTION.value
    assert result.evidence["field"] == "vehicle_count"


def test_data_corruption_malformed_non_numeric_value():
    payload = _telemetry("power", {"load": 50.0, "voltage": "INVALID", "frequency": 50.0})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.DATA_CORRUPTION.value
    assert result.severity == Severity.CRITICAL.value
    assert result.evidence["field"] == "voltage"
    assert result.evidence["value"] == "INVALID"


def test_data_corruption_nan_value():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": float("nan")})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.DATA_CORRUPTION.value
    assert result.severity == Severity.CRITICAL.value
    assert result.evidence["field"] == "frequency"


def test_data_corruption_infinite_value():
    payload = _telemetry("water", {"pressure": 4.0, "flow_rate": float("inf"), "reservoir_level": 70.0})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.DATA_CORRUPTION.value
    assert result.severity == Severity.CRITICAL.value
    assert result.evidence["field"] == "flow_rate"


def test_data_corruption_invalid_signal_state():
    payload = _telemetry("traffic", {"vehicle_count": 40, "average_speed": 45.0, "signal_state": "UNKNOWN"})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.DATA_CORRUPTION.value
    assert result.severity == Severity.LOW.value
    assert result.evidence["field"] == "signal_state"


def test_data_corruption_beyond_overload_ceiling_is_still_corruption():
    # Beyond even the overload band's own max (250) -> corruption, not overload.
    payload = _telemetry("water", {"pressure": 4.0, "flow_rate": 900.0, "reservoir_level": 70.0})
    result = detect_telemetry(payload)

    assert result.fault_type == FaultType.DATA_CORRUPTION.value
    assert result.evidence["field"] == "flow_rate"


# --- NODE_OVERLOAD ---


def test_node_overload_power_load():
    payload = _telemetry("power", {"load": 97.5, "voltage": 230.0, "frequency": 50.0})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.NODE_OVERLOAD.value
    assert result.severity == Severity.MEDIUM.value
    assert result.evidence["field"] == "load"


def test_node_overload_water_flow_rate():
    payload = _telemetry("water", {"pressure": 4.0, "flow_rate": 180.0, "reservoir_level": 70.0})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.NODE_OVERLOAD.value
    assert result.evidence["field"] == "flow_rate"


def test_node_overload_water_pressure():
    payload = _telemetry("water", {"pressure": 7.5, "flow_rate": 50.0, "reservoir_level": 70.0})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.NODE_OVERLOAD.value
    assert result.evidence["field"] == "pressure"


def test_node_overload_traffic_vehicle_count():
    payload = _telemetry("traffic", {"vehicle_count": 300, "average_speed": 2.0, "signal_state": "RED"})
    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.NODE_OVERLOAD.value
    assert result.evidence["field"] == "vehicle_count"


# --- NETWORK_PARTITION (documented limitation) ---


def test_network_partition_hook_fires_when_future_metadata_present():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0})
    payload["network_status"] = "PARTITIONED"

    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.NETWORK_PARTITION.value
    assert result.severity == Severity.HIGH.value


def test_network_partition_is_indistinguishable_from_dropout_without_metadata():
    """Documents the known limitation: with today's schema (no network-state
    field), a message-shaped-like-a-dropout is classified SENSOR_DROPOUT,
    never NETWORK_PARTITION — see anomaly_detector/README.md."""
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0}, status="OFFLINE")

    result = detect_telemetry(payload)

    assert result.fault_type == FaultType.SENSOR_DROPOUT.value
    assert result.fault_type != FaultType.NETWORK_PARTITION.value


# --- MALFORMED INPUT ---


def test_missing_node_id():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0})
    del payload["node_id"]

    result = detect_telemetry(payload)

    assert result.detected is True
    assert result.fault_type == FaultType.INVALID_INPUT.value
    assert result.severity == Severity.CRITICAL.value
    assert result.node_id is None


def test_missing_subsystem():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0})
    del payload["subsystem"]

    result = detect_telemetry(payload)

    assert result.fault_type == FaultType.INVALID_INPUT.value


def test_missing_timestamp():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0})
    del payload["timestamp"]

    result = detect_telemetry(payload)

    assert result.fault_type == FaultType.INVALID_INPUT.value


def test_unsupported_subsystem():
    payload = _telemetry("gas", {"pressure": 1.0})

    result = detect_telemetry(payload)

    assert result.fault_type == FaultType.INVALID_INPUT.value
    assert "gas" in result.reason


def test_wrong_type_for_node_id():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0})
    payload["node_id"] = 12345

    result = detect_telemetry(payload)

    assert result.fault_type == FaultType.INVALID_INPUT.value


def test_payload_not_a_dict():
    result = detect_telemetry(["not", "a", "dict"])

    assert result.detected is True
    assert result.fault_type == FaultType.INVALID_INPUT.value
    assert result.node_id is None


def test_data_wrong_type_is_treated_as_dropout():
    payload = _telemetry("power", "not-a-dict")

    result = detect_telemetry(payload)

    assert result.fault_type == FaultType.SENSOR_DROPOUT.value


# --- Result quality ---


def test_result_always_has_reason_and_evidence():
    payloads = [
        _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0}),
        _telemetry("power", {"load": 999.0, "voltage": 230.0, "frequency": 50.0}),
        _telemetry("power", None),
        {},
    ]
    for payload in payloads:
        result = detect_telemetry(payload)
        assert isinstance(result, DetectionResult)
        assert isinstance(result.reason, str) and result.reason
        assert isinstance(result.evidence, dict)


def test_to_dict_is_json_shaped():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0})
    result = detect_telemetry(payload)
    as_dict = result.to_dict()

    assert set(as_dict.keys()) == {
        "detected",
        "fault_type",
        "severity",
        "node_id",
        "subsystem",
        "timestamp",
        "reason",
        "evidence",
    }


def test_detector_never_raises_on_wildly_malformed_input():
    wild_payloads = [None, 42, "a string", [], {"node_id": None, "subsystem": None}]
    for payload in wild_payloads:
        result = detect_telemetry(payload)
        assert isinstance(result, DetectionResult)
        assert result.fault_type == FaultType.INVALID_INPUT.value
