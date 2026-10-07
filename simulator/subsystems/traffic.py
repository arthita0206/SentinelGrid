"""Simulated traffic sensor node."""

import random

from simulator.node import BaseNode
from shared.utils import clamp

SIGNAL_STATES = ["RED", "YELLOW", "GREEN"]


class TrafficNode(BaseNode):
    def __init__(self, node_id: str):
        super().__init__(node_id, subsystem="traffic")
        self._vehicle_count = random.uniform(10.0, 80.0)
        self._average_speed = random.uniform(20.0, 60.0)

    def _generate_readings(self) -> dict:
        self._vehicle_count = clamp(
            self._vehicle_count + random.uniform(-5.0, 5.0), 0.0, 200.0
        )
        self._average_speed = clamp(
            self._average_speed + random.uniform(-3.0, 3.0), 0.0, 120.0
        )

        return {
            "vehicle_count": int(round(self._vehicle_count)),
            "average_speed": round(self._average_speed, 2),
            "signal_state": random.choice(SIGNAL_STATES),
        }
