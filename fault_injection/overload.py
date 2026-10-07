"""Generates abnormally high burst readings for node-overload faults (FR-2.4).

This only rewrites the numbers a node reports — it does not consume real
CPU/memory or simulate an actual denial-of-service condition.
"""

import random
from typing import Callable, Dict


def _overload_power(data: dict, rng: random.Random) -> dict:
    overloaded = dict(data)
    overloaded["load"] = round(rng.uniform(95.0, 100.0), 2)
    return overloaded


def _overload_water(data: dict, rng: random.Random) -> dict:
    overloaded = dict(data)
    overloaded["flow_rate"] = round(rng.uniform(150.0, 250.0), 2)
    overloaded["pressure"] = round(rng.uniform(7.0, 9.0), 2)
    return overloaded


def _overload_traffic(data: dict, rng: random.Random) -> dict:
    overloaded = dict(data)
    overloaded["vehicle_count"] = rng.randint(250, 400)
    overloaded["average_speed"] = round(rng.uniform(0.0, 5.0), 2)
    return overloaded


OVERLOADERS: Dict[str, Callable[[dict, random.Random], dict]] = {
    "power": _overload_power,
    "water": _overload_water,
    "traffic": _overload_traffic,
}
