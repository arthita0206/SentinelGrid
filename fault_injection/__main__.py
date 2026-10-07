"""Manual fault-injection CLI for local demonstration/testing.

Runs a short local simulation and manually injects one fault into it, so
the effect is directly observable in the JSON telemetry printed to stdout.

Examples:
    python -m fault_injection dropout power-001 --duration 15
    python -m fault_injection corruption water-003
    python -m fault_injection partition traffic-001,traffic-002
    python -m fault_injection overload subsystem:power --ticks 3
"""

import argparse

from shared.config import load_config
from simulator.city_simulator import CitySimulator
from simulator.publisher import LocalPublisher

# Fallback durations used when --duration is not given, matching
# config/simulation_config.yaml's fault_injection defaults.
DEFAULT_DURATIONS = {
    "dropout": 15,
    "corruption": None,
    "partition": 15,
    "overload": 10,
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Manually inject a fault into a local SentinelGrid simulation run."
    )
    parser.add_argument("fault_type", choices=["dropout", "corruption", "partition", "overload"])
    parser.add_argument("target", help="node id, comma-separated node ids, or subsystem:<name>")
    parser.add_argument("--duration", type=float, default=None, help="fault duration in seconds")
    parser.add_argument("--ticks", type=int, default=5, help="number of telemetry ticks to run")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    simulator = CitySimulator(config, LocalPublisher())

    duration = args.duration if args.duration is not None else DEFAULT_DURATIONS[args.fault_type]
    fault = simulator.fault_engine.inject(args.fault_type, args.target, duration_seconds=duration)

    duration_desc = f"duration={duration}s" if duration is not None else "one-shot"
    print(
        f"[fault_injection] Injected {fault.fault_type.value} fault {fault.fault_id} "
        f"targeting {fault.target_node_ids} ({duration_desc})"
    )

    simulator.run(max_ticks=args.ticks)


if __name__ == "__main__":
    main()
