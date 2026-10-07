"""Unit tests for the CitySimulator's node creation and config handling."""

from shared.config import load_config
from simulator.city_simulator import CitySimulator
from simulator.publisher import Publisher
from simulator.subsystems.power import PowerNode
from simulator.subsystems.water import WaterNode
from simulator.subsystems.traffic import TrafficNode


class RecordingPublisher(Publisher):
    """Test double that stores telemetry instead of printing it."""

    def __init__(self):
        self.published = []

    def publish(self, telemetry):
        self.published.append(telemetry)


def test_default_config_creates_20_nodes_per_subsystem():
    config = load_config()
    simulator = CitySimulator(config, RecordingPublisher())

    power_nodes = [n for n in simulator.nodes if isinstance(n, PowerNode)]
    water_nodes = [n for n in simulator.nodes if isinstance(n, WaterNode)]
    traffic_nodes = [n for n in simulator.nodes if isinstance(n, TrafficNode)]

    assert len(power_nodes) == 20
    assert len(water_nodes) == 20
    assert len(traffic_nodes) == 20
    assert len(simulator.nodes) == 60


def test_default_telemetry_interval_is_5_seconds():
    config = load_config()
    assert config["telemetry"]["interval_seconds"] == 5


def test_custom_config_node_counts_are_respected():
    custom_config = {
        "power": {"num_nodes": 2},
        "water": {"num_nodes": 3},
        "traffic": {"num_nodes": 1},
        "telemetry": {"interval_seconds": 1},
    }
    simulator = CitySimulator(custom_config, RecordingPublisher())

    assert len([n for n in simulator.nodes if isinstance(n, PowerNode)]) == 2
    assert len([n for n in simulator.nodes if isinstance(n, WaterNode)]) == 3
    assert len([n for n in simulator.nodes if isinstance(n, TrafficNode)]) == 1


def test_generate_all_telemetry_publishes_one_reading_per_node():
    custom_config = {
        "power": {"num_nodes": 2},
        "water": {"num_nodes": 2},
        "traffic": {"num_nodes": 2},
        "telemetry": {"interval_seconds": 1},
    }
    publisher = RecordingPublisher()
    simulator = CitySimulator(custom_config, publisher)

    readings = simulator.generate_all_telemetry()

    assert len(readings) == 6
    node_ids = {reading.node_id for reading in readings}
    assert len(node_ids) == 6
