"""Unit tests for the local Fault Injection Engine (Step 3)."""

import time

from fault_injection.engine import FaultInjectionEngine
from fault_injection.logger import FaultLogger
from fault_injection.faults import FaultStatus, FaultType
from shared.config import load_config
from simulator.subsystems.power import PowerNode
from simulator.subsystems.water import WaterNode
from simulator.subsystems.traffic import TrafficNode


def _make_engine(tmp_path, config=None):
    logger = FaultLogger(log_path=tmp_path / "fault_log.jsonl")
    engine = FaultInjectionEngine(config or {}, logger=logger)
    return engine, logger


def _is_valid_power(data):
    try:
        return (
            0.0 <= data["load"] <= 100.0
            and 200.0 <= data["voltage"] <= 250.0
            and 49.0 <= data["frequency"] <= 51.0
        )
    except TypeError:
        return False


def _is_valid_water(data):
    try:
        return (
            0.0 <= data["pressure"] <= 8.0
            and 0.0 <= data["flow_rate"] <= 120.0
            and 0.0 <= data["reservoir_level"] <= 100.0
        )
    except TypeError:
        return False


def _is_valid_traffic(data):
    try:
        return (
            data["vehicle_count"] >= 0
            and 0.0 <= data["average_speed"] <= 120.0
            and data["signal_state"] in {"RED", "YELLOW", "GREEN"}
        )
    except TypeError:
        return False


# --- Sensor dropout (FR-2.1) ---


def test_sensor_dropout_blocks_then_restores_publishing(tmp_path):
    node = PowerNode("power-001")
    engine, _ = _make_engine(tmp_path)
    engine.register_nodes([node])

    assert engine.apply(node.generate_telemetry()) is not None

    fault = engine.inject("dropout", "power-001", duration_seconds=0.05)
    assert fault.status == FaultStatus.ACTIVE
    assert engine.apply(node.generate_telemetry()) is None

    time.sleep(0.1)
    assert engine.apply(node.generate_telemetry()) is not None


def test_dropout_does_not_remove_the_node(tmp_path):
    node = PowerNode("power-001")
    engine, _ = _make_engine(tmp_path)
    engine.register_nodes([node])

    engine.inject("dropout", "power-001", duration_seconds=10)

    # The node still exists and keeps generating telemetry internally —
    # only publishing is suppressed by the fault engine.
    assert node.node_id == "power-001"
    assert node.generate_telemetry() is not None


# --- Data corruption (FR-2.2) ---


def test_data_corruption_invalidates_power_reading(tmp_path):
    node = PowerNode("power-001")
    engine, _ = _make_engine(tmp_path, config={"random_seed": 7})
    engine.register_nodes([node])

    assert _is_valid_power(node.generate_telemetry().data)

    engine.inject("corruption", "power-001")
    corrupted = engine.apply(node.generate_telemetry())

    assert corrupted is not None
    assert not _is_valid_power(corrupted.data)


def test_data_corruption_invalidates_water_and_traffic(tmp_path):
    cases = [
        (WaterNode("water-001"), _is_valid_water),
        (TrafficNode("traffic-001"), _is_valid_traffic),
    ]
    for node, is_valid in cases:
        engine, _ = _make_engine(tmp_path, config={"random_seed": 3})
        engine.register_nodes([node])
        engine.inject("corruption", node.node_id)
        corrupted = engine.apply(node.generate_telemetry())
        assert corrupted is not None
        assert not is_valid(corrupted.data)


def test_data_corruption_is_one_shot_without_explicit_duration(tmp_path):
    node = PowerNode("power-001")
    engine, _ = _make_engine(tmp_path, config={"random_seed": 5})
    engine.register_nodes([node])

    fault = engine.inject("corruption", "power-001")
    engine.apply(node.generate_telemetry())

    assert fault.status == FaultStatus.COMPLETED
    # A later reading is no longer corrupted by the (now completed) fault.
    assert _is_valid_power(engine.apply(node.generate_telemetry()).data)


# --- Network partition (FR-2.3) ---


def test_network_partition_blocks_targeted_nodes_only(tmp_path):
    power_nodes = [PowerNode(f"power-{i:03d}") for i in range(1, 4)]
    engine, _ = _make_engine(tmp_path)
    engine.register_nodes(power_nodes)

    engine.inject("partition", "power-001,power-002", duration_seconds=5)

    results = {node.node_id: engine.apply(node.generate_telemetry()) for node in power_nodes}

    assert results["power-001"] is None
    assert results["power-002"] is None
    assert results["power-003"] is not None


# --- Node overload (FR-2.4) ---


def test_node_overload_produces_abnormally_high_readings(tmp_path):
    node = PowerNode("power-001")
    engine, _ = _make_engine(tmp_path, config={"random_seed": 1})
    engine.register_nodes([node])

    normal = node.generate_telemetry().data
    assert normal["load"] <= 100.0

    engine.inject("overload", "power-001", duration_seconds=5)
    overloaded = engine.apply(node.generate_telemetry())

    assert overloaded is not None
    assert overloaded.data["load"] >= 95.0


# --- Fault logging (FR-2.6) ---


def test_fault_injection_creates_log_entry(tmp_path):
    node = PowerNode("power-001")
    engine, logger = _make_engine(tmp_path)
    engine.register_nodes([node])

    fault = engine.inject("dropout", "power-001", duration_seconds=10)
    entries = logger.read_entries()

    assert len(entries) == 1
    entry = entries[0]
    assert entry["fault_id"] == fault.fault_id
    assert entry["fault_type"] == FaultType.SENSOR_DROPOUT.value
    assert entry["target_nodes"] == ["power-001"]
    assert "injected_at" in entry
    assert entry["injected_at"].endswith("+00:00")
    assert entry["status"] == FaultStatus.ACTIVE.value


# --- Targeting ---


def test_single_node_targeting(tmp_path):
    nodes = [PowerNode(f"power-{i:03d}") for i in range(1, 3)]
    engine, _ = _make_engine(tmp_path)
    engine.register_nodes(nodes)

    fault = engine.inject("dropout", "power-001", duration_seconds=5)
    assert fault.target_node_ids == ["power-001"]


def test_multi_node_targeting(tmp_path):
    nodes = [PowerNode(f"power-{i:03d}") for i in range(1, 4)]
    engine, _ = _make_engine(tmp_path)
    engine.register_nodes(nodes)

    fault = engine.inject("dropout", "power-001,power-002", duration_seconds=5)
    assert fault.target_node_ids == ["power-001", "power-002"]


def test_subsystem_targeting(tmp_path):
    water_nodes = [WaterNode(f"water-{i:03d}") for i in range(1, 4)]
    power_nodes = [PowerNode("power-001")]
    engine, _ = _make_engine(tmp_path)
    engine.register_nodes(water_nodes + power_nodes)

    fault = engine.inject("partition", "subsystem:water", duration_seconds=5)
    assert set(fault.target_node_ids) == {"water-001", "water-002", "water-003"}


# --- Configuration ---


def test_fault_injection_disabled_by_default_in_config():
    config = load_config()
    assert config["fault_injection"]["enabled"] is False


def test_default_fault_injection_config_values():
    fi_config = load_config()["fault_injection"]
    assert fi_config["dropout"]["duration_seconds"] == 15
    assert fi_config["partition"]["duration_seconds"] == 15
    assert fi_config["overload"]["duration_seconds"] == 10
    for section in ("dropout", "corruption", "partition", "overload"):
        assert fi_config[section]["probability"] == 0.0


# --- Random mode reproducibility ---


def test_random_seed_makes_triggering_reproducible(tmp_path):
    config = {
        "enabled": True,
        "mode": "random",
        "random_seed": 123,
        "dropout": {"enabled": True, "duration_seconds": 5, "probability": 1.0},
        "corruption": {"enabled": False, "probability": 0.0},
        "partition": {"enabled": False, "probability": 0.0},
        "overload": {"enabled": False, "probability": 0.0},
    }

    engine_a, _ = _make_engine(tmp_path, config=dict(config))
    engine_a.register_nodes([PowerNode(f"power-{i:03d}") for i in range(1, 4)])
    engine_a.tick(elapsed_seconds=0.0)

    engine_b, _ = _make_engine(tmp_path, config=dict(config))
    engine_b.register_nodes([PowerNode(f"power-{i:03d}") for i in range(1, 4)])
    engine_b.tick(elapsed_seconds=0.0)

    targets_a = sorted(f.target_node_ids[0] for f in engine_a.active_faults)
    targets_b = sorted(f.target_node_ids[0] for f in engine_b.active_faults)
    assert targets_a == targets_b
    assert len(targets_a) > 0
