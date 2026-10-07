#!/usr/bin/env python3
"""Step 36 controlled live performance test — publish phase.

Publishes a small, statistically useful sample of controlled MEDIUM
NODE_OVERLOAD telemetry events through the REAL, unmodified
IoT -> SQS -> SentinelGridSqsTelemetryConsumer -> SentinelGridAnomalyDetector
path (the same `load=97.8` overload value already proven, in Steps
29/33/34, to classify as NODE_OVERLOAD/MEDIUM and trigger automatic
remediation) — never invoking the Analyzer, Step Functions, or any
DynamoDB write directly.

Each event uses a unique `node_id` and its own generation timestamp, and
events are spaced apart (`--spacing-seconds`, default 20s) so CloudWatch/
Step Functions/Logs evidence for one event is never ambiguous with
another's, per Step 36's explicit "avoid simultaneous events initially"
requirement.

This script ONLY publishes and records what it published — it does not
read any CloudWatch metric/log, does not call DynamoDB, and does not call
Step Functions. See `scripts/step36_analyze_results.py` for the read-only
analysis phase, run separately after the pipeline has had time to finish
processing every event.

Usage:
    python3 scripts/step36_live_performance_test.py [--count 10] \\
        [--spacing-seconds 20] [--output /path/to/events.json]

Requires the `sentinelgrid` AWS CLI profile (ap-south-1) to already be
authenticated — this script makes exactly one AWS API call per event:
`iot-data:Publish` to the existing `sentinelgrid/telemetry` topic.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lambda_functions.remediation_trigger.trigger import compute_execution_name  # noqa: E402
from shared.telemetry import Telemetry  # noqa: E402
from simulator.aws_publisher import AwsIotPublisher  # noqa: E402

PROFILE = "sentinelgrid"
REGION = "ap-south-1"
TOPIC = "sentinelgrid/telemetry"
SUBSYSTEM = "power"
FAULT_TYPE = "NODE_OVERLOAD"

# load=97.8 sits inside the power/load overload band (95.0, 100.0) --
# see lambda_functions/anomaly_detector/detector.py's NUMERIC_FIELD_SPECS
# -- and is the exact value used by every prior controlled MEDIUM
# NODE_OVERLOAD live test (Steps 29/33/34). voltage/frequency stay within
# their normal bands so only the overload condition is exercised.
OVERLOAD_DATA = {"load": 97.8, "voltage": 230.0, "frequency": 50.0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10, help="Number of controlled events to publish.")
    parser.add_argument("--spacing-seconds", type=float, default=20.0, help="Delay between publishes.")
    parser.add_argument(
        "--output",
        type=str,
        default=str(REPO_ROOT / "docs" / "step36_live_test_events.json"),
        help="Where to write the published-event record (input to the analysis script).",
    )
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    publisher = AwsIotPublisher(region=REGION, topic=TOPIC, profile=PROFILE)

    events = []
    print(f"SentinelGrid Step 36 live performance test -- run_id={run_id}")
    print(f"Publishing {args.count} controlled MEDIUM NODE_OVERLOAD events, {args.spacing_seconds}s apart.")
    print(f"Topic: {TOPIC}  Region: {REGION}  Profile: {PROFILE}\n")

    for i in range(args.count):
        node_id = f"power-step36-perf-{i:02d}-{run_id}"
        telemetry_timestamp = datetime.now(timezone.utc).isoformat()
        telemetry = Telemetry(
            node_id=node_id,
            subsystem=SUBSYSTEM,
            timestamp=telemetry_timestamp,
            status="ONLINE",
            data=dict(OVERLOAD_DATA),
        )

        publish_wall_clock_before = datetime.now(timezone.utc).isoformat()
        publisher.publish(telemetry)
        publish_wall_clock_after = datetime.now(timezone.utc).isoformat()

        execution_name = compute_execution_name(node_id, telemetry_timestamp, FAULT_TYPE)

        record = {
            "index": i,
            "node_id": node_id,
            "telemetry_timestamp": telemetry_timestamp,
            "publish_wall_clock_before": publish_wall_clock_before,
            "publish_wall_clock_after": publish_wall_clock_after,
            "predicted_execution_name": execution_name,
            "subsystem": SUBSYSTEM,
            "fault_type": FAULT_TYPE,
        }
        events.append(record)
        print(f"  [{i + 1}/{args.count}] published node_id={node_id} timestamp={telemetry_timestamp}")

        if i < args.count - 1:
            time.sleep(args.spacing_seconds)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"run_id": run_id, "count": args.count, "spacing_seconds": args.spacing_seconds, "events": events}, f, indent=2)

    print(f"\nAll {args.count} events published. Record written to: {output_path}")
    print("Wait for the pipeline to finish processing the last event before running the analysis script.")


if __name__ == "__main__":
    main()
