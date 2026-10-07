"""Unit tests for tools/performance_analysis.py (Step 36 — performance
measurement and validation foundation).

These tests never touch AWS — every function under test is a pure
computation over plain values/dicts already shaped like what boto3 would
return, so no credentials, network access, or live AWS calls are ever
required.
"""

from datetime import datetime, timezone

import pytest

from tools.performance_analysis import (
    PhaseLatencies,
    compute_phase_latencies,
    elapsed_ms,
    extract_state_timings,
    parse_lambda_report_line,
    percentile,
    summarize,
)


# --- elapsed_ms: timestamp extraction / validation ---


def test_elapsed_ms_normal_case_with_iso_strings():
    assert elapsed_ms("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:05+00:00") == 5000.0


def test_elapsed_ms_normal_case_with_datetimes():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 0, 0, 1, 500000, tzinfo=timezone.utc)
    assert elapsed_ms(start, end) == 1500.0


def test_elapsed_ms_mixed_string_and_datetime():
    end = datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
    assert elapsed_ms("2026-01-01T00:00:00+00:00", end) == 5000.0


def test_elapsed_ms_missing_start_returns_none():
    assert elapsed_ms(None, "2026-01-01T00:00:05+00:00") is None


def test_elapsed_ms_missing_end_returns_none():
    assert elapsed_ms("2026-01-01T00:00:00+00:00", None) is None


def test_elapsed_ms_malformed_start_returns_none_not_raises():
    assert elapsed_ms("not-a-timestamp", "2026-01-01T00:00:05+00:00") is None


def test_elapsed_ms_malformed_end_returns_none_not_raises():
    assert elapsed_ms("2026-01-01T00:00:00+00:00", "not-a-timestamp") is None


def test_elapsed_ms_future_start_after_end_returns_none_not_negative():
    # start is AFTER end -- clock skew / out-of-order evidence.
    result = elapsed_ms("2026-01-01T00:00:10+00:00", "2026-01-01T00:00:00+00:00")
    assert result is None


def test_elapsed_ms_naive_datetime_treated_as_utc():
    start = datetime(2026, 1, 1, 0, 0, 0)  # no tzinfo
    end = datetime(2026, 1, 1, 0, 0, 2)
    assert elapsed_ms(start, end) == 2000.0


def test_elapsed_ms_zero_elapsed_is_valid_not_none():
    assert elapsed_ms("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00") == 0.0


# --- parse_lambda_report_line ---


def test_parse_report_line_warm_invocation_no_init_duration():
    line = (
        "REPORT RequestId: 714d4ba1-dafe-4ab0-bc32-4717f596d252\t"
        "Duration: 42.53 ms\tBilled Duration: 43 ms\t"
        "Memory Size: 128 MB\tMax Memory Used: 96 MB\t"
    )
    parsed = parse_lambda_report_line(line)
    assert parsed["request_id"] == "714d4ba1-dafe-4ab0-bc32-4717f596d252"
    assert parsed["duration_ms"] == 42.53
    assert parsed["billed_duration_ms"] == 43.0
    assert parsed["memory_size_mb"] == 128
    assert parsed["max_memory_used_mb"] == 96
    assert parsed["init_duration_ms"] is None
    assert parsed["is_cold_start"] is False


def test_parse_report_line_cold_start_has_init_duration():
    line = (
        "REPORT RequestId: ab388d47-3815-43c0-9e00-ed2a03d86acb\t"
        "Duration: 2217.73 ms\tBilled Duration: 2453 ms\t"
        "Memory Size: 128 MB\tMax Memory Used: 96 MB\t"
        "Init Duration: 235.18 ms\t"
    )
    parsed = parse_lambda_report_line(line)
    assert parsed["init_duration_ms"] == 235.18
    assert parsed["is_cold_start"] is True


def test_parse_report_line_non_report_line_returns_none():
    assert parse_lambda_report_line("START RequestId: abc Version: $LATEST") is None
    assert parse_lambda_report_line("[INFO] some application log line") is None
    assert parse_lambda_report_line("") is None


def test_parse_report_line_never_raises_on_garbage():
    assert parse_lambda_report_line("REPORT completely malformed garbage") is None


# --- percentile ---


def test_percentile_single_value():
    assert percentile([42.0], 95) == 42.0


def test_percentile_known_values():
    values = list(range(1, 11))  # 1..10
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 10.0
    assert percentile(values, 50) == pytest.approx(5.5)


def test_percentile_empty_raises_value_error():
    with pytest.raises(ValueError):
        percentile([], 90)


def test_percentile_order_independent():
    assert percentile([5, 1, 3, 2, 4], 50) == percentile([1, 2, 3, 4, 5], 50)


# --- summarize ---


def test_summarize_basic_statistics():
    result = summarize([10.0, 20.0, 30.0, 40.0, 50.0])
    assert result["count"] == 5
    assert result["min"] == 10.0
    assert result["max"] == 50.0
    assert result["mean"] == 30.0
    assert result["median"] == 30.0
    assert "p90" in result
    assert "p95" in result


def test_summarize_empty_sample_returns_count_zero_only():
    result = summarize([])
    assert result == {"count": 0}


def test_summarize_with_threshold_counts_correctly():
    result = summarize([5.0, 9.0, 11.0, 15.0], threshold_ms=10.0)
    assert result["count_within_threshold"] == 2
    assert result["pct_within_threshold"] == 50.0


def test_summarize_all_within_threshold():
    result = summarize([1.0, 2.0, 3.0], threshold_ms=100.0)
    assert result["pct_within_threshold"] == 100.0


def test_summarize_none_within_threshold():
    result = summarize([200.0, 300.0], threshold_ms=100.0)
    assert result["pct_within_threshold"] == 0.0


def test_summarize_without_threshold_omits_threshold_keys():
    result = summarize([1.0, 2.0])
    assert "threshold_ms" not in result
    assert "pct_within_threshold" not in result


# --- extract_state_timings ---


def _sf_event(event_type, timestamp, **details):
    event = {"type": event_type, "timestamp": timestamp}
    if event_type == "TaskStateEntered":
        event["stateEnteredEventDetails"] = {"name": details.get("name")}
    elif event_type == "TaskStateExited":
        event["stateExitedEventDetails"] = {"name": details.get("name")}
    return event


def test_extract_state_timings_full_execution():
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc)

    events = [
        {"type": "ExecutionStarted", "timestamp": t0},
        _sf_event("TaskStateEntered", t1, name="PlanRemediation"),
        _sf_event("TaskStateExited", t2, name="PlanRemediation"),
        _sf_event("TaskStateEntered", t2, name="ExecuteRemediation"),
        _sf_event("TaskStateExited", t3, name="ExecuteRemediation"),
        {"type": "ExecutionSucceeded", "timestamp": t3},
    ]

    result = extract_state_timings(events)
    assert result["execution"]["started"] == t0
    assert result["execution"]["stopped"] == t3
    assert result["states"]["PlanRemediation"]["entered"] == t1
    assert result["states"]["PlanRemediation"]["exited"] == t2
    assert result["states"]["ExecuteRemediation"]["entered"] == t2
    assert result["states"]["ExecuteRemediation"]["exited"] == t3


def test_extract_state_timings_missing_state_is_absent_not_error():
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    events = [{"type": "ExecutionStarted", "timestamp": t0}, {"type": "ExecutionSucceeded", "timestamp": t0}]
    result = extract_state_timings(events)
    assert "VerifyRemediation" not in result["states"]


def test_extract_state_timings_empty_events_returns_none_execution():
    result = extract_state_timings([])
    assert result["execution"]["started"] is None
    assert result["execution"]["stopped"] is None
    assert result["states"] == {}


def test_extract_state_timings_failed_execution_uses_execution_failed():
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    events = [
        {"type": "ExecutionStarted", "timestamp": t0},
        {"type": "ExecutionFailed", "timestamp": t1},
    ]
    result = extract_state_timings(events)
    assert result["execution"]["stopped"] == t1


# --- compute_phase_latencies ---


def test_compute_phase_latencies_full_evidence():
    telemetry_ts = "2026-01-01T00:00:00+00:00"
    analyzer_report_time = datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
    execution_start = datetime(2026, 1, 1, 0, 0, 6, tzinfo=timezone.utc)
    execution_stop = datetime(2026, 1, 1, 0, 0, 8, tzinfo=timezone.utc)

    state_timings = {
        "execution": {"started": execution_start, "stopped": execution_stop},
        "states": {
            "PlanRemediation": {"entered": execution_start, "exited": datetime(2026, 1, 1, 0, 0, 6, 500000, tzinfo=timezone.utc)},
            "ExecuteRemediation": {
                "entered": datetime(2026, 1, 1, 0, 0, 6, 500000, tzinfo=timezone.utc),
                "exited": execution_stop,
            },
        },
    }

    result = compute_phase_latencies(
        telemetry_timestamp=telemetry_ts,
        analyzer_report_time=analyzer_report_time,
        analyzer_duration_ms=42.5,
        execution_start=execution_start,
        execution_stop=execution_stop,
        state_timings=state_timings,
    )

    assert isinstance(result, PhaseLatencies)
    assert result.telemetry_to_detection_ms == 5000.0
    assert result.detection_processing_ms == 42.5
    assert result.detection_to_remediation_start_ms == 1000.0
    assert result.remediation_workflow_ms == 2000.0
    assert result.telemetry_to_recovery_ms == 8000.0
    assert result.planner_duration_ms == 500.0
    assert result.executor_duration_ms == 1500.0
    assert result.verifier_duration_ms is None  # VerifyRemediation never ran


def test_compute_phase_latencies_missing_evidence_produces_none_fields_not_crash():
    result = compute_phase_latencies(telemetry_timestamp="2026-01-01T00:00:00+00:00")
    assert result.telemetry_to_detection_ms is None
    assert result.detection_processing_ms is None
    assert result.detection_to_remediation_start_ms is None
    assert result.remediation_workflow_ms is None
    assert result.telemetry_to_recovery_ms is None
    assert result.planner_duration_ms is None


def test_compute_phase_latencies_partial_evidence_computes_what_it_can():
    telemetry_ts = "2026-01-01T00:00:00+00:00"
    analyzer_report_time = datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)

    result = compute_phase_latencies(
        telemetry_timestamp=telemetry_ts,
        analyzer_report_time=analyzer_report_time,
        analyzer_duration_ms=10.0,
        # no execution_start/execution_stop -- e.g. no remediation was triggered
    )

    assert result.telemetry_to_detection_ms == 5000.0
    assert result.detection_processing_ms == 10.0
    assert result.detection_to_remediation_start_ms is None
    assert result.remediation_workflow_ms is None
    assert result.telemetry_to_recovery_ms is None


def test_compute_phase_latencies_malformed_telemetry_timestamp_is_safe():
    result = compute_phase_latencies(
        telemetry_timestamp="garbage-not-a-timestamp",
        analyzer_report_time=datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
        analyzer_duration_ms=10.0,
    )
    assert result.telemetry_to_detection_ms is None
    assert result.detection_processing_ms == 10.0  # unaffected by the other field's malformed input
