"""Generates clearly invalid or malformed telemetry values (FR-2.2).

Each function takes a normal reading dict and returns a corrupted copy —
either a numerically impossible value or a malformed/invalid one — so the
corruption is unambiguous rather than a borderline high-but-plausible
reading.
"""

import random
from typing import Callable, Dict

MALFORMED_MARKER = "INVALID"


def _corrupt_power(data: dict, rng: random.Random) -> dict:
    corrupted = dict(data)
    mode = rng.choice(["voltage", "frequency", "load", "malformed"])
    if mode == "voltage":
        corrupted["voltage"] = round(rng.uniform(500.0, 800.0), 2)
    elif mode == "frequency":
        corrupted["frequency"] = round(rng.uniform(90.0, 150.0), 2)
    elif mode == "load":
        corrupted["load"] = round(rng.uniform(150.0, 300.0), 2)
    else:
        corrupted["voltage"] = MALFORMED_MARKER
    return corrupted


def _corrupt_water(data: dict, rng: random.Random) -> dict:
    corrupted = dict(data)
    mode = rng.choice(["pressure", "reservoir_level", "flow_rate", "malformed"])
    if mode == "pressure":
        corrupted["pressure"] = round(rng.uniform(-10.0, -1.0), 2)
    elif mode == "reservoir_level":
        corrupted["reservoir_level"] = round(rng.uniform(150.0, 300.0), 2)
    elif mode == "flow_rate":
        corrupted["flow_rate"] = round(rng.uniform(-100.0, -10.0), 2)
    else:
        corrupted["reservoir_level"] = MALFORMED_MARKER
    return corrupted


def _corrupt_traffic(data: dict, rng: random.Random) -> dict:
    corrupted = dict(data)
    mode = rng.choice(["vehicle_count", "average_speed", "signal_state", "malformed"])
    if mode == "vehicle_count":
        corrupted["vehicle_count"] = rng.randint(-500, -1)
    elif mode == "average_speed":
        corrupted["average_speed"] = round(rng.uniform(500.0, 1000.0), 2)
    elif mode == "signal_state":
        corrupted["signal_state"] = "UNKNOWN"
    else:
        corrupted["vehicle_count"] = MALFORMED_MARKER
    return corrupted


CORRUPTORS: Dict[str, Callable[[dict, random.Random], dict]] = {
    "power": _corrupt_power,
    "water": _corrupt_water,
    "traffic": _corrupt_traffic,
}
