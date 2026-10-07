#!/usr/bin/env python3
"""Step 37 controlled live scale test — publish phase (SRS NFR-3).

Publishes NORMAL (non-anomalous) telemetry for all 500 virtual nodes
defined by `config/scale_test_config.yaml`, through the REAL, unmodified
`IoT Core -> sentinelgrid/telemetry -> SentinelGridTelemetryQueue (SQS) /
sentinelgrid_telemetry_to_dynamodb (direct)` paths -- never invoking any
Lambda, Step Functions execution, or DynamoDB write directly. No fault is
injected by this script (`fault_injection.enabled: false` in the scale
config) -- this test measures INGESTION/PROCESSING scalability, not
remediation behavior; see `scripts/step37_fault_sample.py` for the small,
separate, optional fault sample (Step 37 Phase G).

Uses `simulator.publisher.ConcurrentPublisher` (Step 37 addition) to fan
each tick's ~500 `iot-data:Publish` calls across a thread pool -- a
single-threaded sequential loop was measured (see
docs/scale-validation.md Phase A) at ~40-90ms per call, which would make
one tick take 20-45 seconds sequentially, far exceeding the configured
5-second interval. `CitySimulator` itself is NOT modified to be
concurrent -- this script calls `CitySimulator.generate_all_telemetry()`
directly (already a plain, existing, public method) and hands the result
to `ConcurrentPublisher.publish_many()`, entirely outside
`CitySimulator.run()`'s own default sequential per-tick loop, which stays
exactly as it always was for every other caller.

This script ONLY publishes and records what it published/observed
locally (per-tick success/failure counts, timing) — it does not read any
CloudWatch metric/log, does not call DynamoDB, and does not call Step
Functions. See `scripts/step37_analyze_scale_results.py` for the
read-only analysis phase.

Usage:
    python3 scripts/step37_scale_test.py [--ticks 12] [--interval-seconds 5] \\
        [--max-workers 25] [--output docs/step37_scale_test_events.json] \\
        [--config config/scale_test_config.yaml]

Step 38 addition: `--config` lets a re-run point at a different config
file (e.g. `config/scale_test_config_step38.yaml`, identical to Step
37's own config except for `node_id_infix`) without touching this
script's code or hardcoding a new identifier here -- default is
unchanged, so a bare re-run of this command reproduces Step 37 exactly.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.config import load_config  # noqa: E402
from simulator.aws_publisher import AwsIotPublisher  # noqa: E402
from simulator.city_simulator import CitySimulator  # noqa: E402
from simulator.publisher import ConcurrentPublisher  # noqa: E402

SCALE_CONFIG_PATH = REPO_ROOT / "config" / "scale_test_config.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=12, help="Number of telemetry ticks to run.")
    parser.add_argument("--interval-seconds", type=float, default=5.0, help="Target seconds between tick starts.")
    parser.add_argument("--max-workers", type=int, default=25, help="ConcurrentPublisher thread pool size.")
    parser.add_argument(
        "--output",
        type=str,
        default=str(REPO_ROOT / "docs" / "step37_scale_test_events.json"),
        help="Where to write the run summary (input to the analysis script).",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(SCALE_CONFIG_PATH),
        help=(
            "Path to a scale-test config YAML (same shape as config/scale_test_config.yaml). "
            "Default is Step 37's own config, unchanged. Added in Step 38 so a re-run (e.g. "
            "config/scale_test_config_step38.yaml, with only node_id_infix changed) never needs "
            "a code change or a hardcoded identifier -- see that file's own comments."
        ),
    )
    args = parser.parse_args()

    config = load_config(args.config)
    aws_config = config.get("aws") or {}
    iot_config = aws_config.get("iot") or {}

    aws_publisher = AwsIotPublisher(
        region=aws_config.get("region"),
        topic=iot_config.get("topic"),
        profile=aws_config.get("profile"),
        endpoint_url=iot_config.get("endpoint_url"),
    )
    publisher = ConcurrentPublisher(aws_publisher, max_workers=args.max_workers)

    simulator = CitySimulator(config, publisher)
    total_nodes = len(simulator.nodes)
    node_ids = sorted(n.node_id for n in simulator.nodes)

    print("SentinelGrid Step 37 live scale test (SRS NFR-3)")
    print(f"  total nodes: {total_nodes}  ticks: {args.ticks}  interval: {args.interval_seconds}s  workers: {args.max_workers}")
    print(f"  expected total telemetry events: {total_nodes * args.ticks}")
    print(f"  node_id_infix: {config.get('node_id_infix')!r}\n")

    test_start = datetime.now(timezone.utc).isoformat()
    tick_records = []

    for tick in range(args.ticks):
        tick_start_monotonic = time.monotonic()
        tick_start_wall = datetime.now(timezone.utc).isoformat()

        readings = simulator.generate_all_telemetry()
        result = publisher.publish_many(readings)

        tick_elapsed = time.monotonic() - tick_start_monotonic
        tick_records.append(
            {
                "tick": tick,
                "tick_start_wall_clock": tick_start_wall,
                "reading_count": len(readings),
                "publish_success": result["success"],
                "publish_failure": result["failure"],
                "publish_elapsed_seconds": round(tick_elapsed, 3),
            }
        )
        print(
            f"  tick {tick + 1}/{args.ticks}: {len(readings)} readings, "
            f"{result['success']} ok / {result['failure']} failed, "
            f"published in {tick_elapsed:.2f}s"
        )

        remaining = args.interval_seconds - tick_elapsed
        if tick < args.ticks - 1 and remaining > 0:
            time.sleep(remaining)

    test_end = datetime.now(timezone.utc).isoformat()

    summary = {
        "test_start": test_start,
        "test_end": test_end,
        "config_path": str(SCALE_CONFIG_PATH),
        "node_id_infix": config.get("node_id_infix"),
        "total_nodes": total_nodes,
        "power_nodes": config["power"]["num_nodes"],
        "water_nodes": config["water"]["num_nodes"],
        "traffic_nodes": config["traffic"]["num_nodes"],
        "ticks": args.ticks,
        "interval_seconds": args.interval_seconds,
        "max_workers": args.max_workers,
        "expected_total_events": total_nodes * args.ticks,
        "actual_publish_success": publisher.success_count,
        "actual_publish_failure": publisher.failure_count,
        "publish_failures": publisher.failures,
        "node_ids": node_ids,
        "tick_records": tick_records,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nTest complete. Published {publisher.success_count} ok / {publisher.failure_count} failed "
          f"of {total_nodes * args.ticks} expected events.")
    print(f"Run summary written to: {output_path}")


if __name__ == "__main__":
    main()
