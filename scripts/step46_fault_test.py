#!/usr/bin/env python3
"""Step 46 controlled live fault-injection test — publish phase (SRS FR-4/FR-5).

Publishes telemetry for exactly ONE virtual node, with exactly ONE
fault injected against it via the EXISTING `fault_injection.engine.
FaultInjectionEngine` (no new fault-injection framework, no new fault
types), through the REAL, unmodified `IoT Core -> sentinelgrid/telemetry
-> SentinelGridTelemetryQueue (SQS) -> SentinelGridSqsTelemetryConsumer
-> SentinelGridAnomalyDetector -> ...` path — never invoking any Lambda,
Step Functions execution, or DynamoDB write directly (that would defeat
the point of an end-to-end test). This is `scripts/step37_scale_test.py`'s
sibling for a SINGLE isolated node rather than 500 — same primitives
(`CitySimulator`, `FaultInjectionEngine`, `AwsIotPublisher`), same real
path, deliberately tiny footprint (Step 46 Phase 3: "approximately 4-6
carefully isolated fault cases", "unique node IDs", "avoid overlapping
faults").

**Ground truth, not assumption**: this script ALSO calls the exact same
`lambda_functions.anomaly_detector.detector.detect_telemetry()` function
the deployed Lambda runs, locally, on each exact payload dict actually
published, and records that as `local_expected_detection` alongside what
was published. This is an independent, code-derived expected value
(computed from the same known, unit-tested module) to compare the live
AWS-observed classification against in the analysis phase -- not a guess.

Only ONE node is created (a 1-node CitySimulator config for the target
subsystem only), so no unrelated node's telemetry is published by this
script at all.

Usage:
    python3 scripts/step46_fault_test.py \\
        --fault-type overload --subsystem power \\
        --node-infix step46-caseA-overload-medium \\
        --ticks 3 --interval-seconds 5 \\
        --output docs/step46_caseA_events.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lambda_functions.anomaly_detector.detector import detect_telemetry  # noqa: E402
from simulator.aws_publisher import AwsIotPublisher  # noqa: E402
from simulator.city_simulator import CitySimulator  # noqa: E402

SUBSYSTEM_NODE_COUNTS = {"power": 0, "water": 0, "traffic": 0}


def build_config(subsystem: str, node_infix: str, interval_seconds: float) -> dict:
    counts = dict(SUBSYSTEM_NODE_COUNTS)
    counts[subsystem] = 1
    return {
        "power": {"num_nodes": counts["power"]},
        "water": {"num_nodes": counts["water"]},
        "traffic": {"num_nodes": counts["traffic"]},
        "telemetry": {"interval_seconds": interval_seconds},
        "node_id_infix": node_infix,
        # mode: manual -- no automatic/random/scheduled triggering; the
        # ONLY fault that ever fires is the one this script injects
        # explicitly via engine.inject() below.
        "fault_injection": {"enabled": False, "mode": "manual"},
        "aws": {
            "region": "ap-south-1",
            "profile": "sentinelgrid",
            "iot": {"topic": "sentinelgrid/telemetry", "endpoint_url": None},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault-type", required=True, choices=["dropout", "corruption", "partition", "overload"])
    parser.add_argument("--subsystem", required=True, choices=["power", "water", "traffic"])
    parser.add_argument("--node-infix", required=True, help="Unique node_id_infix for this single-node test.")
    parser.add_argument("--ticks", type=int, default=3, help="Number of telemetry ticks to publish.")
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--fault-duration-seconds", type=float, default=None, help="None = one-shot (applies once).")
    parser.add_argument("--output", type=str, required=True)
    args = parser.parse_args()

    config = build_config(args.subsystem, args.node_infix, args.interval_seconds)

    aws_config = config["aws"]
    iot_config = aws_config["iot"]
    publisher = AwsIotPublisher(
        region=aws_config.get("region"),
        topic=iot_config.get("topic"),
        profile=aws_config.get("profile"),
        endpoint_url=iot_config.get("endpoint_url"),
    )

    simulator = CitySimulator(config, publisher)
    assert len(simulator.nodes) == 1, f"expected exactly 1 node, got {len(simulator.nodes)}"
    target_node_id = simulator.nodes[0].node_id

    print("SentinelGrid Step 46 live fault test (SRS FR-4/FR-5)")
    print(f"  fault_type: {args.fault_type}  target_node: {target_node_id}  subsystem: {args.subsystem}")
    print(f"  ticks: {args.ticks}  interval: {args.interval_seconds}s  fault_duration_seconds: {args.fault_duration_seconds}")

    injection_start_wall = datetime.now(timezone.utc).isoformat()
    fault = simulator.fault_engine.inject(
        args.fault_type, target_node_id, duration_seconds=args.fault_duration_seconds
    )
    print(f"  injected fault_id={fault.fault_id} at {injection_start_wall}\n")

    published_records = []
    import time as _time

    for tick in range(args.ticks):
        simulator.fault_engine.tick(elapsed_seconds=tick * args.interval_seconds)
        readings = simulator.generate_all_telemetry()  # [] if the fault blocks publishing this tick
        for telemetry in readings:
            payload = telemetry.to_dict()
            publish_ok = publisher.publish(telemetry)
            local_expected = detect_telemetry(payload)
            record = {
                "tick": tick,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "publish_success": publish_ok,
                "payload": payload,
                "local_expected_detection": local_expected.to_dict(),
            }
            published_records.append(record)
            print(
                f"  tick {tick + 1}/{args.ticks}: published={publish_ok} "
                f"local_expected={local_expected.fault_type} severity={local_expected.severity}"
            )
        if not readings:
            print(f"  tick {tick + 1}/{args.ticks}: no telemetry published (fault blocks publishing)")
        if tick < args.ticks - 1:
            _time.sleep(args.interval_seconds)

    test_end_wall = datetime.now(timezone.utc).isoformat()

    summary = {
        "test_id": args.node_infix,
        "fault_type_injected": args.fault_type,
        "subsystem": args.subsystem,
        "target_node_id": target_node_id,
        "fault_id": fault.fault_id,
        "injection_start": injection_start_wall,
        "test_end": test_end_wall,
        "ticks": args.ticks,
        "interval_seconds": args.interval_seconds,
        "fault_duration_seconds": args.fault_duration_seconds,
        "published_records": published_records,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nTest complete. {len(published_records)} telemetry record(s) published for node {target_node_id}.")
    print(f"Run summary written to: {output_path}")


if __name__ == "__main__":
    main()
