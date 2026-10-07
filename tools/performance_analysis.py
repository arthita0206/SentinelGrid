"""Performance measurement analysis tooling for SentinelGrid (Step 36).

This module answers Step 36's core question — "can we cleanly separate
the phases of the telemetry -> detection -> remediation pipeline and
measure each one?" — using ONLY evidence AWS already produces natively:

  - the telemetry event's own `timestamp` field (already used by
    `DetectionLatency`/`RecoveryLatency` — see shared/metrics.py).
  - CloudWatch Logs `REPORT` lines that every AWS Lambda invocation
    already emits (RequestId, Duration, Billed Duration, Init Duration —
    the last one present only on a cold start).
  - AWS Step Functions `DescribeExecution`/`GetExecutionHistory`, which
    records the exact entered/exited wall-clock timestamp of every state
    in `step_functions/remediation_workflow.json` automatically, with no
    application code involved.

**No new CloudWatch metric, Lambda code change, or ASL change was found
to be necessary** to build this performance model — see
`docs/performance-validation.md`'s "Why no new instrumentation" section
for the full reasoning. Every function in this module is a pure
computation over already-fetched data (plain values, `datetime` objects,
or the raw dict shapes `boto3` already returns for
`logs:FilterLogEvents`/`stepfunctions:GetExecutionHistory`) — nothing
here makes an AWS call itself, so everything is unit-testable without
live AWS credentials, and this module is never imported by, or bundled
into, any deployed Lambda package.

This is intentionally independent of `shared/metrics.py`: that module's
`compute_elapsed_ms(start_iso, end=None)` is shaped specifically for "one
ISO string, end defaults to now" (its one real production use case,
called from `anomaly_detector/handler.py`/`remediation/executor.py`).
This module needs elapsed time between two arbitrary, already-known
endpoints (a Step Functions `datetime`, a CloudWatch Logs event
timestamp, ISO strings, or any mix) — `elapsed_ms` below covers that
general case, deliberately re-implementing (not importing)
`compute_elapsed_ms`'s same safety contract (malformed/negative -> `None`
+ a logged warning, never an exception) so this analysis tool can never
crash on a single bad data point mid-report.
"""

import logging
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

TimeLike = Union[str, datetime, None]

# Matches the fixed-format line every AWS Lambda invocation emits to
# CloudWatch Logs, e.g.:
#   "REPORT RequestId: abc-123\tDuration: 42.53 ms\tBilled Duration: 43 ms\t"
#   "Memory Size: 128 MB\tMax Memory Used: 96 MB\t"
# with an optional "Init Duration: 235.18 ms" segment present ONLY when
# that invocation was a cold start (a new execution environment was
# initialized) — its presence/absence is itself useful bottleneck
# evidence (see docs/performance-validation.md Phase G).
_REPORT_LINE_RE = re.compile(
    r"REPORT RequestId:\s*(?P<request_id>[0-9a-fA-F-]+)\s*"
    r"Duration:\s*(?P<duration_ms>[\d.]+)\s*ms\s*"
    r"Billed Duration:\s*(?P<billed_duration_ms>[\d.]+)\s*ms\s*"
    r"Memory Size:\s*(?P<memory_size_mb>\d+)\s*MB\s*"
    r"Max Memory Used:\s*(?P<max_memory_used_mb>\d+)\s*MB"
    r"(\s*Init Duration:\s*(?P<init_duration_ms>[\d.]+)\s*ms)?"
)


def _to_datetime(value: TimeLike) -> Optional[datetime]:
    """Normalizes an ISO-8601 string or an already-`datetime` value into a
    timezone-aware UTC `datetime`. Returns `None` (never raises) for
    `None` input or an unparseable string — mirrors
    `shared.metrics.compute_elapsed_ms`'s own tolerance for malformed
    timestamps.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            logger.warning("SentinelGrid performance analysis: could not parse timestamp %r", value)
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def elapsed_ms(start: TimeLike, end: TimeLike) -> Optional[float]:
    """Milliseconds elapsed from `start` to `end`. Either may be an
    ISO-8601 string, a `datetime`, or `None`.

    Returns `None` (never raises) when:
      - either endpoint is missing or unparseable.
      - the result would be negative (clock skew, or the two events are
        out of the expected order) — publishing/reporting a negative
        "latency" would be actively misleading rather than merely
        absent, matching `shared.metrics.compute_elapsed_ms`'s own rule.
    """
    start_dt = _to_datetime(start)
    end_dt = _to_datetime(end)
    if start_dt is None or end_dt is None:
        return None

    delta_ms = (end_dt - start_dt).total_seconds() * 1000.0
    if delta_ms < 0:
        logger.warning(
            "SentinelGrid performance analysis: start (%s) is after end (%s); skipping "
            "rather than reporting a negative latency.",
            start_dt,
            end_dt,
        )
        return None
    return delta_ms


def parse_lambda_report_line(message: str) -> Optional[Dict[str, Any]]:
    """Parses one CloudWatch Logs `REPORT ...` line (the line every Lambda
    invocation emits automatically) into its fields.

    Returns `None` (never raises) if `message` doesn't match the expected
    `REPORT` shape — e.g. it's an `INIT_START`/`START`/`END` line, or an
    application log line, both of which are common neighbors of a
    `REPORT` line in the same log stream.

    `init_duration_ms` is `None` when absent from the line, which itself
    means this specific invocation was NOT a cold start (a warm execution
    environment was reused) — its presence is a cold-start indicator, not
    an error.
    """
    match = _REPORT_LINE_RE.search(message)
    if not match:
        return None

    init_duration = match.group("init_duration_ms")
    return {
        "request_id": match.group("request_id"),
        "duration_ms": float(match.group("duration_ms")),
        "billed_duration_ms": float(match.group("billed_duration_ms")),
        "memory_size_mb": int(match.group("memory_size_mb")),
        "max_memory_used_mb": int(match.group("max_memory_used_mb")),
        "init_duration_ms": float(init_duration) if init_duration is not None else None,
        "is_cold_start": init_duration is not None,
    }


def percentile(values: List[float], pct: float) -> float:
    """Linear-interpolation percentile (the same method NumPy/Excel
    PERCENTILE.INC use by default) of `values` for `pct` in [0, 100].

    Raises `ValueError` for an empty `values` list — there is no
    meaningful percentile of zero samples, and silently returning e.g.
    `0.0` would be indistinguishable from a real, fast measurement.
    """
    if not values:
        raise ValueError("cannot compute a percentile of an empty sample")
    if len(values) == 1:
        return float(values[0])

    ordered = sorted(values)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = rank - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def summarize(values_ms: List[float], threshold_ms: Optional[float] = None) -> Dict[str, Any]:
    """Computes count/min/max/mean/median/p90/p95 (and, if `threshold_ms`
    is given, the count and percentage of samples at-or-below it) for one
    latency sample.

    Returns `{"count": 0}` (all other keys omitted) for an empty sample
    rather than raising or fabricating a zero-valued statistic — Step 36
    must never claim a distribution exists when no data was collected.
    """
    if not values_ms:
        return {"count": 0}

    result: Dict[str, Any] = {
        "count": len(values_ms),
        "min": min(values_ms),
        "max": max(values_ms),
        "mean": statistics.fmean(values_ms),
        "median": statistics.median(values_ms),
        "p90": percentile(values_ms, 90),
        "p95": percentile(values_ms, 95),
    }
    if threshold_ms is not None:
        count_within = sum(1 for v in values_ms if v <= threshold_ms)
        result["threshold_ms"] = threshold_ms
        result["count_within_threshold"] = count_within
        result["pct_within_threshold"] = 100.0 * count_within / len(values_ms)
    return result


def extract_state_timings(history_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extracts per-state entered/exited timestamps, and the overall
    execution start/stop, from an AWS Step Functions
    `GetExecutionHistory` response's `events` list (the raw boto3 shape —
    each event has `type` and a `timestamp` `datetime`, plus a
    `stateEnteredEventDetails`/`stateExitedEventDetails`/`executionSucceededEventDetails`-
    style sub-dict depending on `type`).

    Returns `{"execution": {"started": dt|None, "stopped": dt|None},
    "states": {state_name: {"entered": dt|None, "exited": dt|None}}}`.
    A state that was never entered (e.g. `ExecuteRemediation` on an
    escalated-without-automatic-remediation execution) is simply absent
    from `"states"` — this is expected, not an error, and callers must
    treat a missing key as "this state did not run in this execution,"
    not as missing data.
    """
    execution_started: Optional[datetime] = None
    execution_stopped: Optional[datetime] = None
    states: Dict[str, Dict[str, Optional[datetime]]] = {}

    for event in history_events:
        event_type = event.get("type")
        timestamp = event.get("timestamp")

        if event_type == "ExecutionStarted":
            execution_started = timestamp
        elif event_type in ("ExecutionSucceeded", "ExecutionFailed", "ExecutionAborted", "ExecutionTimedOut"):
            execution_stopped = timestamp
        elif event_type == "TaskStateEntered" or event_type == "PassStateEntered":
            name = event.get("stateEnteredEventDetails", {}).get("name")
            if name:
                states.setdefault(name, {})["entered"] = timestamp
        elif event_type == "TaskStateExited" or event_type == "PassStateExited":
            name = event.get("stateExitedEventDetails", {}).get("name")
            if name:
                states.setdefault(name, {})["exited"] = timestamp

    return {
        "execution": {"started": execution_started, "stopped": execution_stopped},
        "states": states,
    }


@dataclass
class PhaseLatencies:
    """One test event's latency broken down by pipeline phase, in
    milliseconds. Any field is `None` when the evidence needed to compute
    it was unavailable — never fabricated.

    - `telemetry_to_detection_ms`: telemetry `timestamp` -> the Analyzer
      Lambda invocation's own CloudWatch Logs `REPORT` line time. This is
      an INDEPENDENT reproduction of the existing `DetectionLatency`
      CloudWatch metric (computed the same way, from the same starting
      field, via a different data source) — see
      docs/performance-validation.md.
    - `detection_processing_ms`: the Analyzer Lambda's own `REPORT`
      `Duration` field — the Analyzer's pure compute time, EXCLUDING SQS
      queueing/polling, Lambda-to-Lambda invoke overhead, and any cold
      start (Init Duration is billed/reported separately by AWS and is
      NOT included in `Duration`).
    - `detection_to_remediation_start_ms`: Analyzer `REPORT` time -> the
      Step Functions execution's `startDate`. Covers the async
      Analyzer -> RemediationTrigger invoke, RemediationTrigger's own
      execution, and `StartExecution` latency.
    - `remediation_workflow_ms`: Step Functions execution `startDate` ->
      `stopDate` — the ASL's own, already-available, authoritative
      measurement of pure workflow duration; nothing here recomputes it.
    - `telemetry_to_recovery_ms`: telemetry `timestamp` -> Step Functions
      execution `stopDate` — an INDEPENDENT reproduction of the existing
      `RecoveryLatency` metric's timestamp semantics (which uses the
      Executor's own completion time; `stopDate` is used here as a very
      close proxy when the Executor is the last real Task state, as it is
      for every `SUCCESS` execution the current ASL can produce).
    - `planner_duration_ms`/`executor_duration_ms`/`verifier_duration_ms`:
      each Task state's own `exited - entered` time, from Step Functions
      execution history — includes that Lambda's invocation overhead
      (cold start, if any) in addition to its own compute time, since
      Step Functions measures wall-clock state duration, not isolated
      Lambda `Duration`.
    """

    telemetry_to_detection_ms: Optional[float] = None
    detection_processing_ms: Optional[float] = None
    detection_to_remediation_start_ms: Optional[float] = None
    remediation_workflow_ms: Optional[float] = None
    telemetry_to_recovery_ms: Optional[float] = None
    planner_duration_ms: Optional[float] = None
    executor_duration_ms: Optional[float] = None
    verifier_duration_ms: Optional[float] = None


def compute_phase_latencies(
    *,
    telemetry_timestamp: TimeLike,
    analyzer_report_time: TimeLike = None,
    analyzer_duration_ms: Optional[float] = None,
    execution_start: TimeLike = None,
    execution_stop: TimeLike = None,
    state_timings: Optional[Dict[str, Dict[str, TimeLike]]] = None,
) -> PhaseLatencies:
    """Computes one event's full `PhaseLatencies` from already-extracted
    evidence (a telemetry timestamp, an Analyzer `REPORT` line's wall-clock
    time + Duration, a Step Functions execution's start/stop, and its
    per-state timings from `extract_state_timings`).

    Every individual phase computation is independent: a missing/malformed
    input for one phase produces `None` for that phase only (via
    `elapsed_ms`'s own safety contract) and never prevents the other
    phases from being computed. This function makes no AWS call itself.
    """
    states = state_timings.get("states", {}) if state_timings else {}

    def _state_duration(name: str) -> Optional[float]:
        entry = states.get(name)
        if not entry:
            return None
        return elapsed_ms(entry.get("entered"), entry.get("exited"))

    return PhaseLatencies(
        telemetry_to_detection_ms=elapsed_ms(telemetry_timestamp, analyzer_report_time),
        detection_processing_ms=analyzer_duration_ms,
        detection_to_remediation_start_ms=elapsed_ms(analyzer_report_time, execution_start),
        remediation_workflow_ms=elapsed_ms(execution_start, execution_stop),
        telemetry_to_recovery_ms=elapsed_ms(telemetry_timestamp, execution_stop),
        planner_duration_ms=_state_duration("PlanRemediation"),
        executor_duration_ms=_state_duration("ExecuteRemediation"),
        verifier_duration_ms=_state_duration("VerifyRemediation"),
    )
