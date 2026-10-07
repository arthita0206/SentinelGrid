"""Unit tests for lambda_functions/knowledge_query/metrics_service.py
(Step 43 — read-only CloudWatch metrics reader for the Knowledge Query
API).

These tests never touch AWS — `MetricsQueryService` is always
constructed with an injected fake/mock CloudWatch client, mirroring
`KnowledgeQueryService`'s own DynamoDB Table injection and
`shared.metrics.MetricsClient`'s own client injection. No AWS
credentials, boto3 clients, or network access are required.
"""

import inspect
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from lambda_functions.knowledge_query import metrics_service as metrics_module
from lambda_functions.knowledge_query.metrics_service import (
    APPROVED_METRICS,
    DEFAULT_WINDOW_SECONDS,
    MAX_WINDOW_SECONDS,
    METRIC_UNITS,
    NAMESPACE,
    MetricsQueryError,
    MetricsQueryService,
    _dimension_combinations,
    _period_for_window,
)


def _service(client=None) -> MetricsQueryService:
    return MetricsQueryService(client=client if client is not None else MagicMock())


def _now():
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _cw_result(query_id, timestamps=None, values=None):
    return {"Id": query_id, "Timestamps": timestamps or [], "Values": values or []}


# --- Approved metric allowlist ---


def test_approved_metrics_is_the_exact_seven_metric_contract():
    assert APPROVED_METRICS == (
        "FaultDetectionCount",
        "DetectionLatency",
        "RecoveryAttemptCount",
        "RecoverySuccessCount",
        "RecoveryFailureCount",
        "RecoveryLatency",
        "UnresolvedFaultCount",
    )


def test_namespace_is_sentinelgrid():
    assert NAMESPACE == "SentinelGrid"


def test_metric_units_cover_every_approved_metric_with_correct_unit():
    assert set(METRIC_UNITS.keys()) == set(APPROVED_METRICS)
    assert METRIC_UNITS["DetectionLatency"] == "Milliseconds"
    assert METRIC_UNITS["RecoveryLatency"] == "Milliseconds"
    for name in APPROVED_METRICS:
        if name not in ("DetectionLatency", "RecoveryLatency"):
            assert METRIC_UNITS[name] == "Count"


def test_get_metrics_never_queries_any_metric_outside_the_allowlist():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    queried_names = {q["MetricStat"]["Metric"]["MetricName"] for q in client.get_metric_data.call_args.kwargs["MetricDataQueries"]}
    assert queried_names == set(APPROVED_METRICS)


def test_get_metrics_never_queries_any_namespace_other_than_sentinelgrid():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    service.get_metrics(start=start, end=end)

    namespaces = {q["MetricStat"]["Metric"]["Namespace"] for q in client.get_metric_data.call_args.kwargs["MetricDataQueries"]}
    assert namespaces == {"SentinelGrid"}


# --- Time window constants ---


def test_default_window_is_one_hour():
    assert DEFAULT_WINDOW_SECONDS == 3600


def test_max_window_is_24_hours():
    assert MAX_WINDOW_SECONDS == 86400


# --- Period selection ---


def test_period_for_window_is_60_seconds_for_windows_up_to_an_hour():
    assert _period_for_window(60) == 60
    assert _period_for_window(900) == 60
    assert _period_for_window(3600) == 60


def test_period_for_window_is_300_seconds_for_windows_up_to_six_hours():
    assert _period_for_window(3601) == 300
    assert _period_for_window(21600) == 300


def test_period_for_window_is_900_seconds_for_windows_up_to_24_hours():
    assert _period_for_window(21601) == 900
    assert _period_for_window(86400) == 900


def test_get_metrics_uses_the_period_matching_the_requested_window():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=6, seconds=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    assert result["period_seconds"] == 900
    periods = {q["MetricStat"]["Period"] for q in client.get_metric_data.call_args.kwargs["MetricDataQueries"]}
    assert periods == {900}


# --- Dimension combinations (no ListMetrics, no arbitrary dimensions) ---


def test_dimension_combinations_with_no_filters_is_full_cross_product():
    combos = _dimension_combinations(None, None)
    assert len(combos) == 15  # 3 subsystems x 5 fault types
    assert ("power", "NODE_OVERLOAD") in combos
    assert ("water", "SENSOR_DROPOUT") in combos


def test_dimension_combinations_with_subsystem_only():
    combos = _dimension_combinations("power", None)
    assert len(combos) == 5
    assert all(s == "power" for s, _f in combos)


def test_dimension_combinations_with_fault_type_only():
    combos = _dimension_combinations(None, "NODE_OVERLOAD")
    assert len(combos) == 3
    assert all(f == "NODE_OVERLOAD" for _s, f in combos)


def test_dimension_combinations_fully_specified_is_a_single_pair():
    combos = _dimension_combinations("power", "NODE_OVERLOAD")
    assert combos == [("power", "NODE_OVERLOAD")]


def test_get_metrics_builds_7_queries_for_a_fully_specified_pair():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    assert len(client.get_metric_data.call_args.kwargs["MetricDataQueries"]) == 7


def test_get_metrics_builds_105_queries_when_unfiltered():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    service.get_metrics(start=start, end=end)

    assert len(client.get_metric_data.call_args.kwargs["MetricDataQueries"]) == 105


# --- CloudWatch GetMetricData request construction ---


def test_get_metric_data_called_with_namespace_start_end_and_scan_by():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    kwargs = client.get_metric_data.call_args.kwargs
    assert kwargs["StartTime"] == start
    assert kwargs["EndTime"] == end
    assert kwargs["ScanBy"] == "TimestampAscending"


def test_get_metric_data_dimensions_use_subsystem_and_fault_type_names_only():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    for q in client.get_metric_data.call_args.kwargs["MetricDataQueries"]:
        names = {d["Name"] for d in q["MetricStat"]["Metric"]["Dimensions"]}
        assert names == {"Subsystem", "FaultType"}


def test_count_metrics_use_sum_statistic_latency_metrics_use_average():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    stats_by_metric = {
        q["MetricStat"]["Metric"]["MetricName"]: q["MetricStat"]["Stat"]
        for q in client.get_metric_data.call_args.kwargs["MetricDataQueries"]
    }
    assert stats_by_metric["FaultDetectionCount"] == "Sum"
    assert stats_by_metric["DetectionLatency"] == "Average"
    assert stats_by_metric["RecoveryLatency"] == "Average"


# --- Multiple series, empty datapoints, unit/dimension preservation ---


def test_multiple_metric_series_returned_and_distinguishable():
    client = MagicMock()
    client.get_metric_data.return_value = {
        "MetricDataResults": [
            _cw_result("m0", [datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)], [2.0]),
            _cw_result("m1", [datetime(2026, 6, 1, 11, 5, tzinfo=timezone.utc)], [150.0]),
        ]
    }
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    by_name = {s["metric_name"]: s for s in result["series"] if s["datapoints"]}
    assert "FaultDetectionCount" in by_name
    assert "DetectionLatency" in by_name
    assert by_name["FaultDetectionCount"]["datapoints"][0]["value"] == 2.0
    assert by_name["DetectionLatency"]["datapoints"][0]["value"] == 150.0


def test_fully_specified_pair_returns_all_seven_metrics_even_when_empty():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    assert len(result["series"]) == 7
    assert all(s["datapoints"] == [] for s in result["series"])
    assert all("summary" not in s for s in result["series"])


def test_unfiltered_query_discards_series_with_no_datapoints():
    client = MagicMock()
    # Only one of 105 queries has data.
    client.get_metric_data.return_value = {
        "MetricDataResults": [_cw_result("m0", [datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)], [1.0])]
    }
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end)

    assert len(result["series"]) == 1


def test_dimensions_are_preserved_exactly_in_the_response():
    client = MagicMock()
    client.get_metric_data.return_value = {
        "MetricDataResults": [_cw_result("m0", [datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)], [1.0])]
    }
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="traffic", fault_type="SENSOR_DROPOUT")

    assert result["series"][0]["dimensions"] == {"Subsystem": "traffic", "FaultType": "SENSOR_DROPOUT"}


def test_unit_is_preserved_exactly_in_the_response():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    units_by_metric = {s["metric_name"]: s["unit"] for s in result["series"]}
    assert units_by_metric["FaultDetectionCount"] == "Count"
    assert units_by_metric["DetectionLatency"] == "Milliseconds"


# --- Timestamp normalization ---


def test_timestamps_are_normalized_to_iso8601_strings():
    client = MagicMock()
    client.get_metric_data.return_value = {
        "MetricDataResults": [_cw_result("m0", [datetime(2026, 6, 1, 11, 30, 0, tzinfo=timezone.utc)], [4.0])]
    }
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    dp = next(s for s in result["series"] if s["datapoints"])["datapoints"][0]
    assert dp["timestamp"] == "2026-06-01T11:30:00+00:00"


def test_naive_timestamps_from_cloudwatch_are_treated_as_utc():
    client = MagicMock()
    client.get_metric_data.return_value = {
        "MetricDataResults": [_cw_result("m0", [datetime(2026, 6, 1, 11, 30, 0)], [4.0])]
    }
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    dp = next(s for s in result["series"] if s["datapoints"])["datapoints"][0]
    assert dp["timestamp"] == "2026-06-01T11:30:00+00:00"


# --- Deterministic sorting ---


def test_datapoints_are_sorted_by_timestamp_ascending_regardless_of_input_order():
    client = MagicMock()
    client.get_metric_data.return_value = {
        "MetricDataResults": [
            _cw_result(
                "m0",
                [
                    datetime(2026, 6, 1, 11, 30, tzinfo=timezone.utc),
                    datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
                ],
                [2.0, 1.0],
            )
        ]
    }
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    dps = next(s for s in result["series"] if s["datapoints"])["datapoints"]
    assert [dp["value"] for dp in dps] == [1.0, 2.0]


def test_series_are_sorted_deterministically_by_metric_order_then_subsystem_then_fault_type():
    client = MagicMock()
    # Give every combo a datapoint so nothing is discarded, to check ordering.
    results = []
    for i in range(105):
        results.append(_cw_result(f"m{i}", [datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)], [1.0]))
    client.get_metric_data.return_value = {"MetricDataResults": results}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end)

    metric_order = {name: i for i, name in enumerate(APPROVED_METRICS)}
    keys = [
        (metric_order[s["metric_name"]], s["dimensions"]["Subsystem"], s["dimensions"]["FaultType"])
        for s in result["series"]
    ]
    assert keys == sorted(keys)


# --- Optional summaries ---


def test_summary_sum_for_count_metrics():
    client = MagicMock()
    client.get_metric_data.return_value = {
        "MetricDataResults": [
            _cw_result(
                "m0",
                [datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc), datetime(2026, 6, 1, 11, 1, tzinfo=timezone.utc)],
                [2.0, 3.0],
            )
        ]
    }
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    fault_detection = next(s for s in result["series"] if s["metric_name"] == "FaultDetectionCount")
    assert fault_detection["summary"] == {"datapoint_count": 2, "sum": 5.0}


def test_summary_average_for_latency_metrics_labeled_average_not_mttr():
    client = MagicMock()
    client.get_metric_data.return_value = {
        "MetricDataResults": [
            _cw_result(
                "m5",  # RecoveryLatency is index 5 in APPROVED_METRICS -> query id "m5" for a fully-specified pair
                [datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc), datetime(2026, 6, 1, 11, 1, tzinfo=timezone.utc)],
                [100.0, 200.0],
            )
        ]
    }
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    recovery_latency = next(s for s in result["series"] if s["metric_name"] == "RecoveryLatency")
    assert recovery_latency["summary"] == {"datapoint_count": 2, "average": 150.0}
    assert "mttr" not in json.dumps(recovery_latency).lower()
    assert "percentile" not in json.dumps(recovery_latency).lower()
    assert "p95" not in json.dumps(recovery_latency).lower()


# --- JSON serialization ---


def test_full_response_is_json_serializable():
    client = MagicMock()
    client.get_metric_data.return_value = {
        "MetricDataResults": [_cw_result("m0", [datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)], [1.0])]
    }
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    result = service.get_metrics(start=start, end=end, subsystem="power", fault_type="NODE_OVERLOAD")

    json.dumps(result)  # must not raise


# --- CloudWatch error handling ---


def test_client_error_raises_metrics_query_error():
    client = MagicMock()
    client.get_metric_data.side_effect = ClientError({"Error": {"Code": "Throttling", "Message": "x"}}, "GetMetricData")
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    with pytest.raises(MetricsQueryError):
        service.get_metrics(start=start, end=end)


def test_botocore_error_raises_metrics_query_error():
    client = MagicMock()
    client.get_metric_data.side_effect = BotoCoreError()
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    with pytest.raises(MetricsQueryError):
        service.get_metrics(start=start, end=end)


# --- Read-only guarantee ---


def test_metrics_service_module_never_calls_a_write_or_alarm_operation():
    source = inspect.getsource(metrics_module)
    for forbidden in (
        "put_metric_data(",
        "put_metric_alarm(",
        "set_alarm_state(",
        "delete_alarms(",
        "delete_metric_stream(",
        "put_metric_stream(",
    ):
        assert forbidden not in source, f"metrics_service.py must never call {forbidden!r} -- read-only violation"


def test_get_metrics_only_ever_calls_get_metric_data_on_the_mock_client():
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    service = _service(client)
    end = _now()
    start = end - timedelta(hours=1)

    service.get_metrics(start=start, end=end)

    client.get_metric_data.assert_called_once()
    client.put_metric_data.assert_not_called()
    client.put_metric_alarm.assert_not_called()


# --- Construction never touches AWS ---


def test_constructing_service_never_touches_aws():
    MetricsQueryService()  # must not raise / must not attempt a real AWS call
