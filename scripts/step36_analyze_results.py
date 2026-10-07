#!/usr/bin/env python3
"""Step 36 controlled live performance test — analysis phase (read-only).

Reads the event record written by `scripts/step36_live_performance_test.py`
and, for each event, pulls ONLY already-existing AWS-native evidence:

  - the Analyzer (`SentinelGridAnomalyDetector`) Lambda's own CloudWatch
    Logs, correlated to this event via its unique `node_id` appearing in
    the Analyzer's existing `_log_result` INFO line, then the immediately
    following `REPORT` line for that same invocation (gives detection
    completion wall-clock time, pure Analyzer compute Duration, and
    whether that invocation was a cold start).
  - the SQS consumer (`SentinelGridSqsTelemetryConsumer`) Lambda's own
    CloudWatch Logs, correlated the same way via its existing "processed
    successfully" INFO line (gives that invocation's own cold-start
    status, for Phase G's bottleneck investigation).
  - the deterministically-predicted Step Functions execution name (see
    `lambda_functions/remediation_trigger/trigger.py:compute_execution_name`)
    — `stepfunctions:DescribeExecution` + `GetExecutionHistory` give the
    exact execution start/stop and every state's entered/exited time,
    with zero guessing.

This script makes ONLY read-only AWS calls (`logs:FilterLogEvents`,
`logs:GetLogEvents`, `stepfunctions:DescribeExecution`,
`stepfunctions:GetExecutionHistory`, `cloudwatch:GetMetricStatistics` for
one optional cross-check). It never publishes telemetry, never invokes a
Lambda, never starts a Step Functions execution, and never writes to
DynamoDB.

Usage:
    python3 scripts/step36_analyze_results.py --input docs/step36_live_test_events.json \\
        [--output docs/step36_live_test_results.json]
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.performance_analysis import (  # noqa: E402
    compute_phase_latencies,
    extract_state_timings,
    parse_lambda_report_line,
    summarize,
)

PROFILE = "sentinelgrid"
REGION = "ap-south-1"
ACCOUNT_ID = "401429382000"

ANALYZER_LOG_GROUP = "/aws/lambda/SentinelGridAnomalyDetector"
SQS_CONSUMER_LOG_GROUP = "/aws/lambda/SentinelGridSqsTelemetryConsumer"
STATE_MACHINE_NAME = "SentinelGridRemediationWorkflow"

DETECTION_THRESHOLD_MS = 10_000.0  # NFR-1
RECOVERY_THRESHOLD_MS = 30_000.0  # NFR-2


def _session():
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def _find_invocation_report(logs_client, log_group: str, correlation_substring: str, start_ms: int, end_ms: int) -> Optional[Dict[str, Any]]:
    """Finds the CloudWatch Logs `REPORT` line for the Lambda invocation
    that logged a line containing `correlation_substring` (here, always a
    unique `node_id`) within [start_ms, end_ms] (epoch milliseconds).

    Returns `{"report": <parsed REPORT dict>, "report_time": <datetime>,
    "correlated_line_time": <datetime>}` or `None` if no matching
    invocation/REPORT line could be found in this window.
    """
    try:
        response = logs_client.filter_log_events(
            logGroupName=log_group,
            startTime=start_ms,
            endTime=end_ms,
            filterPattern=f'"{correlation_substring}"',
        )
    except logs_client.exceptions.ResourceNotFoundException:
        return None

    matches = response.get("events", [])
    if not matches:
        return None

    correlated_event = matches[0]
    log_stream = correlated_event["logStreamName"]
    correlated_time_ms = correlated_event["timestamp"]

    # Scan forward in the same log stream (Lambda writes START/app-logs/
    # END/REPORT sequentially per invocation to one stream) for the next
    # REPORT line after the correlated line.
    stream_response = logs_client.get_log_events(
        logGroupName=log_group,
        logStreamName=log_stream,
        startTime=correlated_time_ms,
        startFromHead=True,
        limit=50,
    )
    for event in stream_response.get("events", []):
        parsed = parse_lambda_report_line(event["message"])
        if parsed is not None:
            from datetime import datetime, timezone

            report_time = datetime.fromtimestamp(event["timestamp"] / 1000.0, tz=timezone.utc)
            correlated_time = datetime.fromtimestamp(correlated_time_ms / 1000.0, tz=timezone.utc)
            return {"report": parsed, "report_time": report_time, "correlated_line_time": correlated_time}

    return None


def _analyze_one_event(session, event: Dict[str, Any]) -> Dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    logs_client = session.client("logs")
    sfn_client = session.client("stepfunctions")

    node_id = event["node_id"]
    telemetry_timestamp = event["telemetry_timestamp"]
    telemetry_dt = datetime.fromisoformat(telemetry_timestamp)

    window_start_ms = int((telemetry_dt - timedelta(seconds=5)).timestamp() * 1000)
    window_end_ms = int((telemetry_dt + timedelta(seconds=90)).timestamp() * 1000)

    analyzer_evidence = _find_invocation_report(logs_client, ANALYZER_LOG_GROUP, node_id, window_start_ms, window_end_ms)
    sqs_consumer_evidence = _find_invocation_report(logs_client, SQS_CONSUMER_LOG_GROUP, node_id, window_start_ms, window_end_ms)

    execution_arn = (
        f"arn:aws:states:{REGION}:{ACCOUNT_ID}:execution:{STATE_MACHINE_NAME}:{event['predicted_execution_name']}"
    )
    execution_summary: Dict[str, Any] = {"execution_arn": execution_arn, "found": False}
    state_timings: Optional[Dict[str, Any]] = None
    execution_status = None

    try:
        describe_response = sfn_client.describe_execution(executionArn=execution_arn)
        execution_status = describe_response.get("status")
        execution_summary["found"] = True
        execution_summary["status"] = execution_status
        execution_summary["start_date"] = describe_response.get("startDate")
        execution_summary["stop_date"] = describe_response.get("stopDate")

        history_response = sfn_client.get_execution_history(executionArn=execution_arn, maxResults=200)
        state_timings = extract_state_timings(history_response.get("events", []))
    except sfn_client.exceptions.ExecutionDoesNotExist:
        execution_summary["found"] = False

    analyzer_report_time = analyzer_evidence["report_time"] if analyzer_evidence else None
    analyzer_duration_ms = analyzer_evidence["report"]["duration_ms"] if analyzer_evidence else None
    analyzer_cold_start = analyzer_evidence["report"]["is_cold_start"] if analyzer_evidence else None

    execution_start = state_timings["execution"]["started"] if state_timings else None
    execution_stop = state_timings["execution"]["stopped"] if state_timings else None

    phases = compute_phase_latencies(
        telemetry_timestamp=telemetry_timestamp,
        analyzer_report_time=analyzer_report_time,
        analyzer_duration_ms=analyzer_duration_ms,
        execution_start=execution_start,
        execution_stop=execution_stop,
        state_timings=state_timings,
    )

    return {
        "node_id": node_id,
        "telemetry_timestamp": telemetry_timestamp,
        "analyzer_found": analyzer_evidence is not None,
        "analyzer_cold_start": analyzer_cold_start,
        "analyzer_report_time": analyzer_report_time.isoformat() if analyzer_report_time else None,
        "analyzer_duration_ms": analyzer_duration_ms,
        "sqs_consumer_found": sqs_consumer_evidence is not None,
        "sqs_consumer_cold_start": (sqs_consumer_evidence["report"]["is_cold_start"] if sqs_consumer_evidence else None),
        "execution_found": execution_summary["found"],
        "execution_status": execution_status,
        "execution_start": execution_start.isoformat() if execution_start else None,
        "execution_stop": execution_stop.isoformat() if execution_stop else None,
        "phases": {
            "telemetry_to_detection_ms": phases.telemetry_to_detection_ms,
            "detection_processing_ms": phases.detection_processing_ms,
            "detection_to_remediation_start_ms": phases.detection_to_remediation_start_ms,
            "remediation_workflow_ms": phases.remediation_workflow_ms,
            "telemetry_to_recovery_ms": phases.telemetry_to_recovery_ms,
            "planner_duration_ms": phases.planner_duration_ms,
            "executor_duration_ms": phases.executor_duration_ms,
            "verifier_duration_ms": phases.verifier_duration_ms,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=str, default=str(REPO_ROOT / "docs" / "step36_live_test_events.json"))
    parser.add_argument("--output", type=str, default=str(REPO_ROOT / "docs" / "step36_live_test_results.json"))
    args = parser.parse_args()

    with open(args.input) as f:
        run_record = json.load(f)

    session = _session()
    per_event_results: List[Dict[str, Any]] = []

    for event in run_record["events"]:
        print(f"Analyzing node_id={event['node_id']} ...")
        result = _analyze_one_event(session, event)
        per_event_results.append(result)
        print(
            f"  analyzer_found={result['analyzer_found']} cold_start={result['analyzer_cold_start']} "
            f"execution_found={result['execution_found']} status={result['execution_status']}"
        )
        print(f"  telemetry_to_detection_ms={result['phases']['telemetry_to_detection_ms']}")
        print(f"  telemetry_to_recovery_ms={result['phases']['telemetry_to_recovery_ms']}")

    detection_values = [r["phases"]["telemetry_to_detection_ms"] for r in per_event_results if r["phases"]["telemetry_to_detection_ms"] is not None]
    recovery_values = [r["phases"]["telemetry_to_recovery_ms"] for r in per_event_results if r["phases"]["telemetry_to_recovery_ms"] is not None]
    workflow_values = [r["phases"]["remediation_workflow_ms"] for r in per_event_results if r["phases"]["remediation_workflow_ms"] is not None]
    detection_to_remediation_values = [
        r["phases"]["detection_to_remediation_start_ms"] for r in per_event_results if r["phases"]["detection_to_remediation_start_ms"] is not None
    ]
    detection_processing_values = [
        r["phases"]["detection_processing_ms"] for r in per_event_results if r["phases"]["detection_processing_ms"] is not None
    ]

    summary = {
        "sample_count": len(per_event_results),
        "detection_latency_ms": summarize(detection_values, threshold_ms=DETECTION_THRESHOLD_MS),
        "recovery_latency_ms": summarize(recovery_values, threshold_ms=RECOVERY_THRESHOLD_MS),
        "remediation_workflow_ms": summarize(workflow_values),
        "detection_to_remediation_start_ms": summarize(detection_to_remediation_values),
        "detection_processing_ms": summarize(detection_processing_values),
        "cold_start_count_analyzer": sum(1 for r in per_event_results if r["analyzer_cold_start"]),
        "cold_start_count_sqs_consumer": sum(1 for r in per_event_results if r["sqs_consumer_cold_start"]),
    }

    output = {"run_id": run_record.get("run_id"), "events": per_event_results, "summary": summary}
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nFull results written to: {output_path}")


if __name__ == "__main__":
    main()
