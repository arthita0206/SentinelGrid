"""Top-level simulation engine: builds nodes and drives telemetry generation.

    CitySimulator -> Telemetry objects -> FaultInjectionEngine -> Publisher

Fault injection is applied between telemetry generation and publishing, so
PowerNode/WaterNode/TrafficNode stay responsible only for normal readings.
"""

import time
from typing import Optional

from shared.config import load_config
from simulator.publisher import Publisher
from simulator.subsystems.power import PowerNode
from simulator.subsystems.water import WaterNode
from simulator.subsystems.traffic import TrafficNode
from fault_injection.engine import FaultInjectionEngine


class CitySimulator:
    def __init__(
        self,
        config: dict,
        publisher: Publisher,
        fault_engine: Optional[FaultInjectionEngine] = None,
    ):
        self.config = config
        self.publisher = publisher
        self.nodes = self._create_nodes()
        self.fault_engine = fault_engine or FaultInjectionEngine(config.get("fault_injection", {}))
        self.fault_engine.register_nodes(self.nodes)

    def _create_nodes(self) -> list:
        # `node_id_infix` (Step 37): an OPTIONAL top-level config key, absent
        # from config/simulation_config.yaml and every config dict used
        # before Step 37 -- `.get()` therefore returns None for all
        # existing callers, producing today's exact "power-001"-style IDs
        # unchanged. Set it (e.g. "step37-scale") to get a distinct,
        # collision-free ID namespace for a bounded test run -- e.g.
        # "power-step37-scale-001" -- without touching the default
        # config or PowerNode/WaterNode/TrafficNode themselves, which
        # still just take whatever id string they're given.
        infix = self.config.get("node_id_infix")

        def _node_id(prefix: str, i: int) -> str:
            return f"{prefix}-{infix}-{i:03d}" if infix else f"{prefix}-{i:03d}"

        nodes = []
        nodes += [
            PowerNode(_node_id("power", i))
            for i in range(1, self.config["power"]["num_nodes"] + 1)
        ]
        nodes += [
            WaterNode(_node_id("water", i))
            for i in range(1, self.config["water"]["num_nodes"] + 1)
        ]
        nodes += [
            TrafficNode(_node_id("traffic", i))
            for i in range(1, self.config["traffic"]["num_nodes"] + 1)
        ]
        return nodes

    def generate_all_telemetry(self) -> list:
        readings = []
        for node in self.nodes:
            telemetry = self.fault_engine.apply(node.generate_telemetry())
            if telemetry is not None:
                readings.append(telemetry)
        return readings

    def run(self, max_ticks: Optional[int] = None) -> None:
        interval = self.config["telemetry"]["interval_seconds"]
        start = time.monotonic()
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                elapsed = time.monotonic() - start
                self.fault_engine.tick(elapsed_seconds=elapsed)

                for telemetry in self.generate_all_telemetry():
                    self.publisher.publish(telemetry)

                ticks += 1
                if max_ticks is None or ticks < max_ticks:
                    time.sleep(interval)
        except KeyboardInterrupt:
            print("\nSentinelGrid simulator stopped.")

    @classmethod
    def from_default_config(cls, publisher: Publisher) -> "CitySimulator":
        return cls(load_config(), publisher)
