"""Unit tests for the Step 37 scale-test additions (SRS NFR-3):
`CitySimulator`'s `node_id_infix` support, `config/scale_test_config.yaml`
itself, and `simulator.publisher.ConcurrentPublisher`.

None of these tests touch AWS — `RecordingPublisher`/`FailingPublisher`
test doubles stand in for `AwsIotPublisher` throughout, matching
tests/test_city_simulator.py's own convention.
"""

import time

import pytest

from shared.config import load_config
from shared.telemetry import Telemetry
from simulator.city_simulator import CitySimulator
from simulator.publisher import ConcurrentPublisher, Publisher
from simulator.subsystems.power import PowerNode
from simulator.subsystems.traffic import TrafficNode
from simulator.subsystems.water import WaterNode

SCALE_CONFIG_PATH = "config/scale_test_config.yaml"


class RecordingPublisher(Publisher):
    """Test double that stores telemetry instead of publishing it -- mirrors
    tests/test_city_simulator.py's own RecordingPublisher exactly."""

    def __init__(self):
        self.published = []

    def publish(self, telemetry):
        self.published.append(telemetry)


class SlowRecordingPublisher(Publisher):
    """Like RecordingPublisher, but sleeps briefly per call -- used to prove
    ConcurrentPublisher.publish_many() is actually concurrent (wall time
    << N * per-call sleep), not merely a sequential loop in disguise."""

    def __init__(self, delay_seconds: float = 0.05):
        self.published = []
        self.delay_seconds = delay_seconds

    def publish(self, telemetry):
        time.sleep(self.delay_seconds)
        self.published.append(telemetry)


class FailingPublisher(Publisher):
    """Raises for every Nth telemetry reading (by node_id suffix), to
    exercise ConcurrentPublisher's per-item failure isolation."""

    def __init__(self, fail_node_ids):
        self.fail_node_ids = set(fail_node_ids)
        self.published = []

    def publish(self, telemetry):
        if telemetry.node_id in self.fail_node_ids:
            raise RuntimeError(f"simulated publish failure for {telemetry.node_id}")
        self.published.append(telemetry)


# --- config/scale_test_config.yaml itself ---


def test_scale_config_file_exists_and_loads():
    config = load_config(SCALE_CONFIG_PATH)
    assert config is not None


def test_scale_config_totals_500_nodes():
    config = load_config(SCALE_CONFIG_PATH)
    total = config["power"]["num_nodes"] + config["water"]["num_nodes"] + config["traffic"]["num_nodes"]
    assert total == 500


def test_scale_config_subsystem_distribution():
    config = load_config(SCALE_CONFIG_PATH)
    assert config["power"]["num_nodes"] == 167
    assert config["water"]["num_nodes"] == 167
    assert config["traffic"]["num_nodes"] == 166


def test_scale_config_uses_a_distinct_node_id_infix():
    config = load_config(SCALE_CONFIG_PATH)
    assert config["node_id_infix"] == "step37-scale"


def test_scale_config_fault_injection_disabled_by_default():
    config = load_config(SCALE_CONFIG_PATH)
    assert config["fault_injection"]["enabled"] is False


def test_scale_config_telemetry_interval_is_5_seconds():
    config = load_config(SCALE_CONFIG_PATH)
    assert config["telemetry"]["interval_seconds"] == 5


def test_default_simulation_config_is_unchanged_by_the_scale_config():
    default_config = load_config()  # config/simulation_config.yaml, no override
    assert default_config["power"]["num_nodes"] == 20
    assert default_config["water"]["num_nodes"] == 20
    assert default_config["traffic"]["num_nodes"] == 20
    assert "node_id_infix" not in default_config


# --- CitySimulator + node_id_infix ---


def test_city_simulator_builds_exactly_500_nodes_from_scale_config():
    config = load_config(SCALE_CONFIG_PATH)
    simulator = CitySimulator(config, RecordingPublisher())
    assert len(simulator.nodes) == 500


def test_city_simulator_scale_config_subsystem_counts():
    config = load_config(SCALE_CONFIG_PATH)
    simulator = CitySimulator(config, RecordingPublisher())
    assert len([n for n in simulator.nodes if isinstance(n, PowerNode)]) == 167
    assert len([n for n in simulator.nodes if isinstance(n, WaterNode)]) == 167
    assert len([n for n in simulator.nodes if isinstance(n, TrafficNode)]) == 166


def test_city_simulator_scale_config_node_ids_are_unique():
    config = load_config(SCALE_CONFIG_PATH)
    simulator = CitySimulator(config, RecordingPublisher())
    node_ids = [n.node_id for n in simulator.nodes]
    assert len(node_ids) == len(set(node_ids)) == 500


def test_city_simulator_scale_config_node_ids_use_the_infix():
    config = load_config(SCALE_CONFIG_PATH)
    simulator = CitySimulator(config, RecordingPublisher())
    assert all("step37-scale" in n.node_id for n in simulator.nodes)
    assert simulator.nodes[0].node_id == "power-step37-scale-001"


def test_city_simulator_without_infix_uses_original_id_format_unchanged():
    # Backward compatibility: a config with no node_id_infix key (e.g. the
    # real default config, or any pre-Step-37 test config) must produce
    # EXACTLY today's "power-001" format -- not "power-None-001" or any
    # other altered shape.
    custom_config = {
        "power": {"num_nodes": 2},
        "water": {"num_nodes": 1},
        "traffic": {"num_nodes": 1},
        "telemetry": {"interval_seconds": 1},
    }
    simulator = CitySimulator(custom_config, RecordingPublisher())
    ids = [n.node_id for n in simulator.nodes]
    assert ids == ["power-001", "power-002", "water-001", "traffic-001"]


def test_city_simulator_default_config_unaffected_by_infix_feature():
    config = load_config()
    simulator = CitySimulator(config, RecordingPublisher())
    assert len(simulator.nodes) == 60
    assert simulator.nodes[0].node_id == "power-001"


def test_generate_all_telemetry_produces_one_reading_per_node_at_scale():
    config = load_config(SCALE_CONFIG_PATH)
    simulator = CitySimulator(config, RecordingPublisher())
    readings = simulator.generate_all_telemetry()
    assert len(readings) == 500
    assert len({r.node_id for r in readings}) == 500


def test_generate_all_telemetry_readings_have_no_faults_when_disabled():
    config = load_config(SCALE_CONFIG_PATH)
    simulator = CitySimulator(config, RecordingPublisher())
    readings = simulator.generate_all_telemetry()
    # fault_injection.enabled=false -> FaultInjectionEngine.apply() never
    # drops (returns None for) or rewrites a reading; every node's own
    # normal telemetry passes through unchanged.
    assert len(readings) == len(simulator.nodes)
    assert all(r.status == "ONLINE" for r in readings)


# --- Determinism (seeding the stdlib `random` module directly) ---


def test_telemetry_values_are_deterministic_with_a_fixed_seed():
    import random

    config = {"power": {"num_nodes": 5}, "water": {"num_nodes": 0}, "traffic": {"num_nodes": 0}, "telemetry": {"interval_seconds": 1}}

    random.seed(1234)
    sim_a = CitySimulator(config, RecordingPublisher())
    readings_a = [r.data for r in sim_a.generate_all_telemetry()]

    random.seed(1234)
    sim_b = CitySimulator(config, RecordingPublisher())
    readings_b = [r.data for r in sim_b.generate_all_telemetry()]

    assert readings_a == readings_b


# --- ConcurrentPublisher ---


def test_concurrent_publisher_rejects_zero_or_negative_max_workers():
    with pytest.raises(ValueError):
        ConcurrentPublisher(RecordingPublisher(), max_workers=0)
    with pytest.raises(ValueError):
        ConcurrentPublisher(RecordingPublisher(), max_workers=-1)


def test_concurrent_publisher_single_publish_delegates_to_inner():
    inner = RecordingPublisher()
    publisher = ConcurrentPublisher(inner, max_workers=4)
    telemetry = Telemetry(node_id="power-001", subsystem="power", timestamp="2026-01-01T00:00:00+00:00", status="ONLINE", data={})

    publisher.publish(telemetry)

    assert inner.published == [telemetry]


def test_concurrent_publisher_publish_many_delivers_every_reading():
    inner = RecordingPublisher()
    publisher = ConcurrentPublisher(inner, max_workers=10)
    readings = [
        Telemetry(node_id=f"power-{i:03d}", subsystem="power", timestamp="2026-01-01T00:00:00+00:00", status="ONLINE", data={})
        for i in range(50)
    ]

    result = publisher.publish_many(readings)

    assert result == {"success": 50, "failure": 0, "total": 50}
    assert len(inner.published) == 50
    assert {r.node_id for r in inner.published} == {r.node_id for r in readings}


def test_concurrent_publisher_publish_many_is_actually_concurrent():
    delay = 0.05
    inner = SlowRecordingPublisher(delay_seconds=delay)
    publisher = ConcurrentPublisher(inner, max_workers=20)
    readings = [
        Telemetry(node_id=f"power-{i:03d}", subsystem="power", timestamp="2026-01-01T00:00:00+00:00", status="ONLINE", data={})
        for i in range(40)
    ]

    start = time.monotonic()
    publisher.publish_many(readings)
    elapsed = time.monotonic() - start

    # Fully sequential would take >= 40 * 0.05s = 2.0s. With 20 workers,
    # two "rounds" of 20 concurrent 0.05s calls should take well under 1s.
    # A generous bound (1.0s) keeps this test robust on a slow CI machine
    # while still clearly proving concurrency, not a sequential loop.
    assert elapsed < 1.0


def test_concurrent_publisher_isolates_per_item_failures():
    fail_ids = {"power-003", "power-017"}
    inner = FailingPublisher(fail_ids)
    publisher = ConcurrentPublisher(inner, max_workers=8)
    readings = [
        Telemetry(node_id=f"power-{i:03d}", subsystem="power", timestamp="2026-01-01T00:00:00+00:00", status="ONLINE", data={})
        for i in range(20)
    ]

    result = publisher.publish_many(readings)

    assert result == {"success": 18, "failure": 2, "total": 20}
    assert len(inner.published) == 18
    failed_node_ids = {node_id for node_id, _ in publisher.failures}
    assert failed_node_ids == fail_ids


def test_concurrent_publisher_never_raises_even_when_every_item_fails():
    inner = FailingPublisher(fail_node_ids={f"power-{i:03d}" for i in range(10)})
    publisher = ConcurrentPublisher(inner, max_workers=5)
    readings = [
        Telemetry(node_id=f"power-{i:03d}", subsystem="power", timestamp="2026-01-01T00:00:00+00:00", status="ONLINE", data={})
        for i in range(10)
    ]

    result = publisher.publish_many(readings)  # must not raise

    assert result == {"success": 0, "failure": 10, "total": 10}


def test_concurrent_publisher_counts_accumulate_across_multiple_batches():
    inner = RecordingPublisher()
    publisher = ConcurrentPublisher(inner, max_workers=5)
    batch = [Telemetry(node_id=f"power-{i:03d}", subsystem="power", timestamp="2026-01-01T00:00:00+00:00", status="ONLINE", data={}) for i in range(5)]

    publisher.publish_many(batch)
    publisher.publish_many(batch)

    assert publisher.success_count == 10
    assert publisher.failure_count == 0


def test_concurrent_publisher_empty_batch_is_a_safe_no_op():
    inner = RecordingPublisher()
    publisher = ConcurrentPublisher(inner, max_workers=5)

    result = publisher.publish_many([])

    assert result == {"success": 0, "failure": 0, "total": 0}
