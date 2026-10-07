"""Simulated power grid node."""

import random

from simulator.node import BaseNode
from shared.utils import clamp


class PowerNode(BaseNode):
    def __init__(self, node_id: str):
        super().__init__(node_id, subsystem="power")
        self._load = random.uniform(30.0, 70.0)
        self._voltage = random.uniform(228.0, 232.0)
        self._frequency = random.uniform(49.9, 50.1)

    def _generate_readings(self) -> dict:
        self._load = clamp(self._load + random.uniform(-2.0, 2.0), 0.0, 100.0)
        self._voltage = clamp(self._voltage + random.uniform(-0.5, 0.5), 200.0, 250.0)
        self._frequency = clamp(self._frequency + random.uniform(-0.05, 0.05), 49.0, 51.0)

        return {
            "load": round(self._load, 2),
            "voltage": round(self._voltage, 2),
            "frequency": round(self._frequency, 3),
        }
