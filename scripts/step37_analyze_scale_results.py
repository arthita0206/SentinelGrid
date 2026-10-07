#!/usr/bin/env python3
"""Step 37 controlled live scale test — analysis phase (read-only, SRS
NFR-3).

Reads the run summary written by `scripts/step37_scale_test.py` and
verifies, using ONLY read-only AWS calls:

  - Every expected (node_id, tick) pair produced exactly one
    `SentinelGridTelemetry` item (completeness + duplicate check), via a
    single paginated `Scan` filtered to this run's unique
    `node_id_infix` — the table's own key is `(node_id, timestamp)`, so
    an item count above `total_nodes * ticks` for our node set would
    mean unexpected duplication, and below would mean missing telemetry.
  - `SentinelGridTelemetryQueue`/`SentinelGridTelemetryDLQ` depths.
  - `SentinelGridSqsTelemetryConsumer`/`SentinelGridAnomalyDetector`
    CloudWatch Lambda metrics (Invocations/Errors/Throttles) for the test
    window.
  - Missing-node detection (which of the 500 expected node_ids have fewer
    than `ticks` telemetry items).

This script makes ONLY read-only AWS calls (`dynamodb:Scan`,
`sqs:GetQueueAttributes`, `cloudwatch:GetMetricStatistics`). It never
publishes telemetry, never invokes a Lambda, never starts a Step
Functions execution, and never writes to DynamoDB.

Usage:
    python3 scripts/step37_analyze_scale_results.py \\
        --input docs/step37_scale_test_events.json \\
        --output docs/step37_scale_test_results.json
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PROFILE = "sentinelgrid"
REGION = "ap-south-1"

TELEMETRY_TABLE = "SentinelGridTelemetry"
QUEUE_URL = "https://sqs.ap-south-1.amazonaws.com/401429382000/SentinelGridTelemetryQueue"
DLQ_URL = "https://sqs.ap-south-1.amazonaws.com/401429382000/SentinelGridTelemetryDLQ"

LAMBDA_FUNCTIONS = ("SentinelGridSqsTelemetryConsumer", "SentinelGridAnomalyDetector")


def _session():
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def _scan_telemetry_for_infix(dynamodb_client, infix: str) -> Dict[str, int]:
    """Scans SentinelGridTelemetry, counting items per node_id whose
    node_id contains `infix` -- returns {node_id: item_count}. A single
    filtered Scan (not 500 individual Query calls) keeps this a small,
    bounded number of AWS calls regardless of node count.
    """
    counts: Counter = Counter()
    kwargs: Dict[str, Any] = {
        "TableName": TELEMETRY_TABLE,
        "FilterExpression": "contains(#nid, :infix)",
        "ExpressionAttributeNames": {"#nid": "node_id"},
        "ExpressionAttributeValues": {":infix": {"S": infix}},
        "ProjectionExpression": "#nid",
    }
    while True:
        response = dynamodb_client.scan(**kwargs)
        for item in response.get("Items", []):
            counts[item["node_id"]["S"]] += 1
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return dict(counts)


def _queue_attributes(sqs_client, queue_url: str) -> Dict[str, str]:
    response = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )
    return response.get("Attributes", {})


def _lambda_metric_sum(cloudwatch_client, function_name: str, metric_name: str, start: datetime, end: datetime) -> float:
    raw_seconds = int((end - start).total_seconds())
    period = max(60, ((raw_seconds + 59) // 60) * 60)  # CloudWatch requires Period to be a multiple of 60
    response = cloudwatch_client.get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName=metric_name,
        Dimensions=[{"Name": "FunctionName", "Value": function_name}],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Sum"],
    )
    datapoints = response.get("Datapoints", [])
    return sum(dp["Sum"] for dp in datapoints)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=str, default=str(REPO_ROOT / "docs" / "step37_scale_test_events.json"))
    parser.add_argument("--output", type=str, default=str(REPO_ROOT / "docs" / "step37_scale_test_results.json"))
    args = parser.parse_args()

    with open(args.input) as f:
        run_record = json.load(f)

    session = _session()
    dynamodb_client = session.client("dynamodb")
    sqs_client = session.client("sqs")
    cloudwatch_client = session.client("cloudwatch")

    infix = run_record["node_id_infix"]
    expected_node_ids = set(run_record["node_ids"])
    ticks = run_record["ticks"]
    expected_total = run_record["expected_total_events"]

    print(f"Scanning {TELEMETRY_TABLE} for node_id_infix={infix!r} ...")
    per_node_counts = _scan_telemetry_for_infix(dynamodb_client, infix)
    actual_total = sum(per_node_counts.values())

    scanned_node_ids = set(per_node_counts.keys())
    missing_nodes = sorted(expected_node_ids - scanned_node_ids)
    unexpected_nodes = sorted(scanned_node_ids - expected_node_ids)

    under_count_nodes = {nid: cnt for nid, cnt in per_node_counts.items() if nid in expected_node_ids and cnt < ticks}
    over_count_nodes = {nid: cnt for nid, cnt in per_node_counts.items() if nid in expected_node_ids and cnt > ticks}

    missing_events = sum(ticks - per_node_counts.get(nid, 0) for nid in expected_node_ids)
    duplicate_events = sum(cnt - ticks for cnt in over_count_nodes.values())

    print(f"  actual total telemetry items for this run: {actual_total} (expected {expected_total})")
    print(f"  nodes with zero telemetry: {len(missing_nodes)}")
    print(f"  nodes under-count (>0 but <{ticks}): {len(under_count_nodes)}")
    print(f"  nodes over-count (>{ticks}, possible duplicates): {len(over_count_nodes)}")

    print("\nChecking SQS queue/DLQ state ...")
    queue_attrs = _queue_attributes(sqs_client, QUEUE_URL)
    dlq_attrs = _queue_attributes(sqs_client, DLQ_URL)
    print(f"  queue: {queue_attrs}")
    print(f"  dlq:   {dlq_attrs}")

    test_start = datetime.fromisoformat(run_record["test_start"])
    test_end = datetime.fromisoformat(run_record["test_end"])
    metric_window_start = test_start - timedelta(minutes=1)
    metric_window_end = datetime.now(timezone.utc) + timedelta(minutes=1)

    print(f"\nChecking Lambda Invocations/Errors/Throttles for {metric_window_start} .. {metric_window_end} ...")
    lambda_metrics: Dict[str, Dict[str, float]] = {}
    for function_name in LAMBDA_FUNCTIONS:
        lambda_metrics[function_name] = {
            "invocations": _lambda_metric_sum(cloudwatch_client, function_name, "Invocations", metric_window_start, metric_window_end),
            "errors": _lambda_metric_sum(cloudwatch_client, function_name, "Errors", metric_window_start, metric_window_end),
            "throttles": _lambda_metric_sum(cloudwatch_client, function_name, "Throttles", metric_window_start, metric_window_end),
        }
        print(f"  {function_name}: {lambda_metrics[function_name]}")

    output = {
        "run_id": infix,
        "expected_total_events": expected_total,
        "actual_total_events": actual_total,
        "completion_pct": round(100.0 * actual_total / expected_total, 3) if expected_total else None,
        "missing_events": missing_events,
        "duplicate_events": duplicate_events,
        "missing_node_count": len(missing_nodes),
        "missing_node_ids": missing_nodes,
        "under_count_nodes": under_count_nodes,
        "over_count_nodes": over_count_nodes,
        "unexpected_node_ids_matching_infix": unexpected_nodes,
        "queue_attributes": queue_attrs,
        "dlq_attributes": dlq_attrs,
        "lambda_metrics": lambda_metrics,
        "test_start": run_record["test_start"],
        "test_end": run_record["test_end"],
        "analysis_time": datetime.now(timezone.utc).isoformat(),
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\n=== SUMMARY ===")
    print(json.dumps(
        {k: v for k, v in output.items() if k not in ("missing_node_ids", "under_count_nodes", "over_count_nodes", "unexpected_node_ids_matching_infix")},
        indent=2,
        default=str,
    ))
    print(f"\nFull results written to: {output_path}")


if __name__ == "__main__":
    main()
