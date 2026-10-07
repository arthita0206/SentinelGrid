"""Simulated water network node."""

import random

from simulator.node import BaseNode
from shared.utils import clamp


class WaterNode(BaseNode):
    def __init__(self, node_id: str):
        super().__init__(node_id, subsystem="water")
        self._pressure = random.uniform(3.0, 5.0)
        self._flow_rate = random.uniform(30.0, 70.0)
        self._reservoir_level = random.uniform(50.0, 90.0)

    def _generate_readings(self) -> dict:
        self._pressure = clamp(self._pressure + random.uniform(-0.2, 0.2), 1.0, 8.0)
        self._flow_rate = clamp(self._flow_rate + random.uniform(-3.0, 3.0), 0.0, 120.0)
        self._reservoir_level = clamp(
            self._reservoir_level + random.uniform(-1.0, 1.0), 0.0, 100.0
        )

        return {
            "pressure": round(self._pressure, 2),
            "flow_rate": round(self._flow_rate, 2),
            "reservoir_level": round(self._reservoir_level, 2),
        }
