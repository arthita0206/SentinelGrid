"""Read-only CloudWatch metrics reader for the Knowledge Query API (Step
43 — SRS FR-7.1/FR-7.2's read path: exposing the metrics `shared/metrics.py`
already publishes, through the same read-only Knowledge Query API the
Nodes/Events pages already use).

This is the ONE module in `lambda_functions/knowledge_query/` that talks
to CloudWatch — `handler.py` never imports boto3 itself, and
`query_service.py` (DynamoDB) never imports it either. This mirrors how
`query_service.py` is the sole DynamoDB-facing module in this package,
and how `shared/metrics.py`'s `MetricsClient` is the sole CloudWatch
*write* adapter elsewhere in the project — this module is its read-side
counterpart. Fully testable without live AWS via a constructor-injected
CloudWatch client (`client=`), mirroring `MetricsClient`'s own injection
pattern.

**Deliberately self-contained, like the rest of this package** (see
`infrastructure/package-knowledge-query.sh`'s own comment: this Lambda
package does NOT import `shared/`, `lambda_functions.anomaly_detector`,
or `lambda_functions.remediation`, only the three DynamoDB tables). Even
though `shared/metrics.py` already defines the `SentinelGrid` namespace
and the seven metric names this module also needs, importing it here
would require repackaging this Lambda to also bundle `shared/` — the
exact "packaging ripple effect" Step 28 hit and documented — for zero
behavioral benefit. This module instead keeps its own independent copy
of the namespace/metric-name/dimension constants, kept in sync BY HAND
with `shared/metrics.py`, exactly the same self-containment tradeoff
`handler.py` already makes for its own subsystem/severity/fault-type
vocabulary copies.

**Strictly read-only**: this module calls `cloudwatch:GetMetricData`
only — never `PutMetricData`, `PutMetricAlarm`, `SetAlarmState`, or
`DeleteAlarms`. See
`tests/test_knowledge_query_metrics_service.py::test_metrics_service_module_never_calls_a_write_or_alarm_operation`,
a static source-scan test mirroring `query_service.py`'s own read-only
guarantee test.

**Approved-metric allowlist, not arbitrary CloudWatch queries**: a caller
can only ever receive data for one or more of the seven `APPROVED_METRICS`
below, in the `SentinelGrid` namespace, dimensioned only by
`Subsystem`/`FaultType` (never `node_id`, never an invented dimension).
There is no way for a request to specify an arbitrary `MetricName` or
`Namespace` — `handler.py` never passes user input into either of those
fields; only `subsystem`/`fault_type` (each independently validated
against this project's own closed vocabularies) select WHICH of the
already-fixed metric/dimension combinations are queried.

**Dimension-combination strategy — no `cloudwatch:ListMetrics` permission
needed**: `SentinelGridNodeState`/the event tables have no equivalent
here — CloudWatch has no way to ask "which Subsystem/FaultType pairs
actually have data" without either `ListMetrics` (an extra permission,
an extra IAM surface, and a second round-trip) or already knowing the
closed set of possible values. This project's domain already has that
closed set (`Subsystem`: power/water/traffic;
`FaultType`: SENSOR_DROPOUT/NETWORK_PARTITION/DATA_CORRUPTION/
NODE_OVERLOAD/INVALID_INPUT — `NONE` excluded, since
`shared/metrics.py`'s `MetricsClient._put` never publishes with a missing
dimension value, and `NONE` is never a real fault), so this module
queries the full cross-product of the (possibly caller-narrowed)
Subsystem/FaultType values × the 7 approved metrics in ONE bounded
`GetMetricData` call, and discards empty results when the caller did not
fully specify both dimensions (see `get_metrics()`'s own docstring for
the exact "when is an empty series still returned" rule this maps
directly to Step 43's own "no datapoint is not necessarily zero"
requirement).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # see anomaly_detector/handler.py for why this is necessary on Lambda

# Kept in sync BY HAND with shared/metrics.py — see module docstring for
# why this module does not import that one directly.
NAMESPACE = "SentinelGrid"
DIMENSION_SUBSYSTEM = "Subsystem"
DIMENSION_FAULT_TYPE = "FaultType"

UNIT_COUNT = "Count"
UNIT_MILLISECONDS = "Milliseconds"

# The fixed, closed allowlist of metric names this endpoint will ever
# query or return — order here is also this module's canonical/display
# order (handler.py's response preserves it). No request input, of any
# kind, ever selects a metric name outside this tuple.
APPROVED_METRICS: tuple = (
    "FaultDetectionCount",
    "DetectionLatency",
    "RecoveryAttemptCount",
    "RecoverySuccessCount",
    "RecoveryFailureCount",
    "RecoveryLatency",
    "UnresolvedFaultCount",
)

# Matches shared/metrics.py's MetricName -> Unit assignment exactly (Count
# for every metric except the two latency ones).
METRIC_UNITS: dict = {
    "FaultDetectionCount": UNIT_COUNT,
    "DetectionLatency": UNIT_MILLISECONDS,
    "RecoveryAttemptCount": UNIT_COUNT,
    "RecoverySuccessCount": UNIT_COUNT,
    "RecoveryFailureCount": UNIT_COUNT,
    "RecoveryLatency": UNIT_MILLISECONDS,
    "UnresolvedFaultCount": UNIT_COUNT,
}

# The project's own closed Subsystem/FaultType vocabularies (mirrors
# handler.py's own independent copies — see that module's comment on why
# these are kept in sync BY HAND rather than imported). Used here ONLY to
# build the dimension cross-product when a caller does not fully specify
# both — never as a target for arbitrary/unvalidated caller input (that
# validation happens in handler.py before this module is ever called).
_ALL_SUBSYSTEMS: tuple = ("power", "water", "traffic")
_ALL_FAULT_TYPES: tuple = ("SENSOR_DROPOUT", "NETWORK_PARTITION", "DATA_CORRUPTION", "NODE_OVERLOAD", "INVALID_INPUT")

DEFAULT_WINDOW_SECONDS = 3600  # 1 hour
MAX_WINDOW_SECONDS = 86400  # 24 hours

# CloudWatch GetMetricData's Period must be a positive multiple of 60.
# shared/metrics.py publishes one raw observation per event -- there is
# no sub-minute resolution to expose, ever (see module docstring). A
# longer window uses a larger period SOLELY to bound how many datapoints
# one response returns (see _period_for_window's own docstring) -- it is
# never used to claim finer precision than 60s actually exists.
_PERIOD_SECONDS_SHORT_WINDOW = 60  # windows <= 1 hour
_PERIOD_SECONDS_MEDIUM_WINDOW = 300  # windows <= 6 hours
_PERIOD_SECONDS_LONG_WINDOW = 900  # windows <= 24 hours

# GetMetricData's own documented hard cap on MetricDataQuery entries per
# call (AWS: up to 500). This module's own maximum possible query count
# is 3 subsystems x 5 fault types x 7 metrics = 105 -- comfortably under
# this ceiling with no pagination or batching ever required. Asserted
# defensively (never expected to trip) rather than silently truncated.
_MAX_METRIC_DATA_QUERIES = 500


class MetricsQueryError(RuntimeError):
    """Raised for a genuine CloudWatch failure while serving a metrics
    query. Mirrors `query_service.KnowledgeQueryError` — never raised for
    "no datapoints", which is a normal, expected outcome (see
    `get_metrics()`'s own docstring), not an error condition.
    """


def _period_for_window(window_seconds: float) -> int:
    """Picks a CloudWatch `Period` (always a multiple of 60) that bounds
    the number of datapoints one series can return to a small, predictable
    number, WITHOUT ever claiming finer resolution than the ~60s the
    underlying metrics actually publish at:
      - <= 1 hour  -> 60s  (<=  60 datapoints/series)
      - <= 6 hours -> 300s (<=  72 datapoints/series)
      - <= 24 hours -> 900s (<=  96 datapoints/series)
    A 60s period at any window is honest (it matches real publish
    resolution); a LARGER period for a longer window is a pre-aggregation
    (CloudWatch's own `Sum`/`Average` statistic over that wider bucket),
    not an attempt to recover false sub-minute precision — the opposite
    concern.
    """
    if window_seconds <= 3600:
        return _PERIOD_SECONDS_SHORT_WINDOW
    if window_seconds <= 21600:
        return _PERIOD_SECONDS_MEDIUM_WINDOW
    return _PERIOD_SECONDS_LONG_WINDOW


def _dimension_combinations(subsystem: Optional[str], fault_type: Optional[str]) -> list:
    """The (Subsystem, FaultType) pairs to query. A caller-supplied value
    narrows that one dimension to a single value; an absent one expands
    to this project's full closed vocabulary (see module docstring's
    "Dimension-combination strategy" for why this needs no `ListMetrics`
    call). Returns a list of `(subsystem, fault_type)` tuples — at most
    3 x 5 = 15 entries.
    """
    subsystems = (subsystem,) if subsystem is not None else _ALL_SUBSYSTEMS
    fault_types = (fault_type,) if fault_type is not None else _ALL_FAULT_TYPES
    return [(s, f) for s in subsystems for f in fault_types]


def _format_timestamp(value: Any) -> str:
    """CloudWatch's GetMetricData returns each datapoint timestamp as a
    native, timezone-aware `datetime` (botocore's own JSON/timestamp
    deserialization — no `decimal.Decimal` handling is needed here the
    way `query_service.py` needs for DynamoDB's Number type, since
    CloudWatch's client-level API returns plain `float`s for metric
    values, never `Decimal`). Rendered as ISO-8601 with an explicit UTC
    offset, matching every other timestamp string this project's APIs
    already return.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


class MetricsQueryService:
    """Read-only reader over the `SentinelGrid` CloudWatch namespace.

    Accepts an optional pre-built CloudWatch client (`client=`) for
    dependency injection — tests pass a `MagicMock()` here, so no real
    AWS call is ever made during `pytest`, mirroring
    `KnowledgeQueryService`'s own table injection and
    `shared.metrics.MetricsClient`'s own client injection. Constructing a
    `MetricsQueryService()` never touches AWS by itself — only the first
    actual query does (lazy client construction).
    """

    def __init__(
        self,
        namespace: str = NAMESPACE,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        client: Optional[Any] = None,
    ):
        self.namespace = namespace
        self.region = region
        self.profile = profile
        self._client = client

    def _get_client(self):
        if self._client is None:
            if self.profile:
                session = boto3.Session(profile_name=self.profile, region_name=self.region)
            else:
                session = boto3.Session(region_name=self.region)
            self._client = session.client("cloudwatch", region_name=self.region)
        return self._client

    def get_metrics(
        self,
        *,
        start: datetime,
        end: datetime,
        subsystem: Optional[str] = None,
        fault_type: Optional[str] = None,
    ) -> dict:
        """Returns `{"period_seconds": int, "series": [...]}` for the
        approved SentinelGrid metrics within `[start, end]`.

        `start`/`end` must already be validated, timezone-aware
        `datetime`s (bounds/parsing validation is `handler.py`'s job,
        exactly like every other query parameter in this package — this
        method trusts its caller the same way `query_service.py`'s
        methods trust `handler.py` for `limit`/`order`).

        **One bounded `GetMetricData` call, never paginated.** This
        method builds one `MetricDataQuery` per (approved metric x
        dimension-combination) pair — at most 105 (see module docstring)
        — and issues exactly one `get_metric_data` call. A `NextToken` in
        the response (should CloudWatch ever return one, which the
        period-tiering above makes very unlikely at this project's data
        volume) is deliberately NOT followed: exhaustively paging through
        more data would defeat the point of a cost-bounded endpoint, so
        this method returns what the first page contains rather than
        looping.

        **Statistic per metric**: `Sum` for Count-unit metrics (the total
        number of published observations in each period bucket — matches
        "FaultDetectionCount: count of detection observations", etc.);
        `Average` for Milliseconds-unit metrics (the mean of whatever raw
        latency observations landed in that period bucket). Never a
        percentile statistic — this project's data does not support one
        (see this package's README/`infrastructure/README.md` for the
        full "do not fabricate p95" reasoning carried over from Step 43's
        own explicit instruction).

        **Empty-series rule — "no datapoint" is not "zero"**: when BOTH
        `subsystem` AND `fault_type` are given (a single, fully-specified
        dimension pair), every one of the 7 approved metrics for that
        exact pair is included in the result, even with an empty
        `datapoints` list — the caller asked for that specific series, so
        an honest "no data in this window" (`datapoints: []`) is returned
        rather than silently omitting the metric or fabricating a zero
        value. When `subsystem`/`fault_type` is absent or only partially
        specified, this method is effectively DISCOVERING "the available
        SentinelGrid metric series for the requested window" across
        multiple unknown dimension pairs — a series with zero datapoints
        in that case is simply omitted from the result (it does not
        exist for this window), keeping the response focused on what is
        actually present rather than listing dozens of empty
        subsystem/fault-type combinations that never published anything.

        Raises `MetricsQueryError` for a genuine CloudWatch failure.
        """
        combos = _dimension_combinations(subsystem, fault_type)
        fully_specified = subsystem is not None and fault_type is not None
        period_seconds = _period_for_window((end - start).total_seconds())

        queries = []
        query_meta = []
        for combo_subsystem, combo_fault_type in combos:
            for metric_name in APPROVED_METRICS:
                query_id = f"m{len(queries)}"
                unit = METRIC_UNITS[metric_name]
                stat = "Sum" if unit == UNIT_COUNT else "Average"
                queries.append(
                    {
                        "Id": query_id,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": self.namespace,
                                "MetricName": metric_name,
                                "Dimensions": [
                                    {"Name": DIMENSION_SUBSYSTEM, "Value": combo_subsystem},
                                    {"Name": DIMENSION_FAULT_TYPE, "Value": combo_fault_type},
                                ],
                            },
                            "Period": period_seconds,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    }
                )
                query_meta.append((metric_name, combo_subsystem, combo_fault_type, unit))

        assert len(queries) <= _MAX_METRIC_DATA_QUERIES, (
            f"metrics_service.py: built {len(queries)} MetricDataQuery entries, "
            f"exceeding the {_MAX_METRIC_DATA_QUERIES} GetMetricData allows per call -- "
            "this should be unreachable given the fixed 3x5x7 domain size."
        )

        try:
            client = self._get_client()
            response = client.get_metric_data(
                MetricDataQueries=queries,
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampAscending",
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("SentinelGrid knowledge query: CloudWatch GetMetricData failed: %s", exc)
            raise MetricsQueryError("Failed to read SentinelGrid metrics from CloudWatch.") from exc

        results_by_id = {result["Id"]: result for result in response.get("MetricDataResults", [])}

        series = []
        for query_id, (metric_name, combo_subsystem, combo_fault_type, unit) in zip(
            (q["Id"] for q in queries), query_meta
        ):
            result = results_by_id.get(query_id, {})
            timestamps = result.get("Timestamps", [])
            values = result.get("Values", [])
            datapoints = [
                {"timestamp": _format_timestamp(ts), "value": value} for ts, value in zip(timestamps, values)
            ]
            # Defensive, deterministic sort -- ScanBy='TimestampAscending'
            # already requests this ordering from CloudWatch, but this
            # method does not rely solely on that request parameter for
            # its own "sort datapoints deterministically" guarantee.
            datapoints.sort(key=lambda dp: dp["timestamp"])

            if not datapoints and not fully_specified:
                continue

            entry = {
                "metric_name": metric_name,
                "unit": unit,
                "dimensions": {DIMENSION_SUBSYSTEM: combo_subsystem, DIMENSION_FAULT_TYPE: combo_fault_type},
                "datapoints": datapoints,
            }
            if datapoints:
                if unit == UNIT_COUNT:
                    entry["summary"] = {
                        "datapoint_count": len(datapoints),
                        "sum": sum(dp["value"] for dp in datapoints),
                    }
                else:
                    entry["summary"] = {
                        "datapoint_count": len(datapoints),
                        "average": sum(dp["value"] for dp in datapoints) / len(datapoints),
                    }
            series.append(entry)

        # Deterministic response ordering: by the metric's own canonical
        # position in APPROVED_METRICS, then Subsystem, then FaultType --
        # never CloudWatch's own (unspecified) result ordering.
        metric_order = {name: index for index, name in enumerate(APPROVED_METRICS)}
        series.sort(key=lambda s: (metric_order[s["metric_name"]], s["dimensions"][DIMENSION_SUBSYSTEM], s["dimensions"][DIMENSION_FAULT_TYPE]))

        return {"period_seconds": period_seconds, "series": series}
