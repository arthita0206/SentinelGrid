"""Unit tests for individual simulated node telemetry."""

from datetime import datetime

from simulator.subsystems.power import PowerNode
from simulator.subsystems.water import WaterNode
from simulator.subsystems.traffic import TrafficNode

REQUIRED_FIELDS = {"node_id", "subsystem", "timestamp", "status", "data"}


def test_power_telemetry_has_required_fields_and_data():
    node = PowerNode("power-001")
    telemetry = node.generate_telemetry().to_dict()

    assert REQUIRED_FIELDS.issubset(telemetry.keys())
    assert telemetry["node_id"] == "power-001"
    assert telemetry["subsystem"] == "power"
    assert telemetry["status"] == "ONLINE"
    assert {"load", "voltage", "frequency"}.issubset(telemetry["data"].keys())


def test_water_telemetry_has_required_fields_and_data():
    node = WaterNode("water-001")
    telemetry = node.generate_telemetry().to_dict()

    assert REQUIRED_FIELDS.issubset(telemetry.keys())
    assert telemetry["subsystem"] == "water"
    assert {"pressure", "flow_rate", "reservoir_level"}.issubset(telemetry["data"].keys())


def test_traffic_telemetry_has_required_fields_and_data():
    node = TrafficNode("traffic-001")
    telemetry = node.generate_telemetry().to_dict()

    assert REQUIRED_FIELDS.issubset(telemetry.keys())
    assert telemetry["subsystem"] == "traffic"
    assert {"vehicle_count", "average_speed", "signal_state"}.issubset(
        telemetry["data"].keys()
    )
    assert telemetry["data"]["signal_state"] in {"RED", "YELLOW", "GREEN"}


def test_timestamp_is_present_and_utc():
    node = PowerNode("power-001")
    telemetry = node.generate_telemetry().to_dict()

    parsed = datetime.fromisoformat(telemetry["timestamp"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
