"""Centralized CloudWatch custom-metric contract for SentinelGrid (Step 28,
SRS FR-7.1's observability foundation).

This module defines the ONE metric contract every AWS-facing Lambda
adapter in this project publishes against, and the ONE CloudWatch client
wrapper used to publish it. See infrastructure/README.md's "CloudWatch
metrics (Step 28)" section for the full metric table (name / unit /
dimensions / owner) and the reasoning behind each choice below.

AWS dependency boundary (mirrors event_store.py /
remediation_trigger_client.py): this module IS an AWS-facing adapter —
it is the one place in this project that imports boto3 for CloudWatch.
The deterministic/business-logic modules this project keeps
AWS-independent (lambda_functions/anomaly_detector/detector.py,
remediation/models.py, remediation/engine.py) do NOT import this module
and never will — every metric is published from an AWS-facing
handler.py-style adapter, never from those pure modules. See each
handler's own module docstring for exactly where it calls in.

Best-effort, not fail-fast (deliberately different from every other AWS
adapter in this project): event_store.py and remediation_trigger_client.py
both fail loudly on a failed AWS call, because a lost anomaly-event write
or a lost remediation-trigger submission would silently break the
self-healing pipeline itself. Metric publication is different — it is
observability ABOUT the pipeline, not part of it — so every `put_*`
method below catches its own failures, logs them, and returns `False`
rather than raising. A CloudWatch outage must never fail a successful
detection, block persistence, or affect Step Functions semantics.

CloudWatch dimensions are deliberately bounded to (Subsystem, FaultType)
— never node_id — to keep custom-metric cardinality low and predictable,
appropriate for this project's low-cost AWS account: the system may
support hundreds of virtual nodes, and a per-node dimension would create
a new CloudWatch metric stream per node.
"""

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

import boto3

logger = logging.getLogger(__name__)
# See lambda_functions/anomaly_detector/handler.py for why this is
# necessary on Lambda: the Python runtime's root logger defaults to
# WARNING, which would otherwise silently drop every logger.info() call
# below.
logger.setLevel(logging.INFO)

NAMESPACE = "SentinelGrid"

DIMENSION_SUBSYSTEM = "Subsystem"
DIMENSION_FAULT_TYPE = "FaultType"


class MetricName:
    """The fixed, closed set of SentinelGrid custom metric names (Step 28).

    See infrastructure/README.md for what each one means, its owner, and
    when it is published — this class only names them, it carries no
    publishing logic itself.
    """

    FAULT_DETECTION_COUNT = "FaultDetectionCount"
    RECOVERY_ATTEMPT_COUNT = "RecoveryAttemptCount"
    RECOVERY_SUCCESS_COUNT = "RecoverySuccessCount"
    RECOVERY_FAILURE_COUNT = "RecoveryFailureCount"
    UNRESOLVED_FAULT_COUNT = "UnresolvedFaultCount"
    DETECTION_LATENCY = "DetectionLatency"
    RECOVERY_LATENCY = "RecoveryLatency"


class Unit:
    COUNT = "Count"
    MILLISECONDS = "Milliseconds"


def compute_elapsed_ms(start_iso: Optional[str], end: Optional[datetime] = None) -> Optional[float]:
    """Elapsed milliseconds from an ISO-8601 timestamp string to `end`
    (defaults to now, UTC). Used for both DetectionLatency
    (telemetry timestamp -> detection time) and RecoveryLatency
    (detection timestamp -> remediation completion time) — see
    infrastructure/README.md for why both currently measure from the
    same underlying field.

    Never raises. Returns `None` — logging why — rather than a
    number, for any input this project cannot safely turn into a
    meaningful, non-misleading latency observation:
      - `start_iso` is missing/empty.
      - `start_iso` is not a parseable ISO-8601 string (malformed
        telemetry must never crash detection — see detector.py's own
        "never raises" contract, which this function preserves).
      - The computed elapsed time is negative (the start timestamp is
        after `end` — clock skew or a future telemetry timestamp).
        Publishing a negative "latency" would be actively misleading
        rather than merely absent, so this is treated the same as an
        unparseable timestamp: skip, log, continue.
    """
    if not start_iso:
        return None

    try:
        start_dt = datetime.fromisoformat(start_iso)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "SentinelGrid metrics: could not parse timestamp %r for latency calculation (error_type=%s)",
            start_iso,
            type(exc).__name__,
        )
        return None

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)

    end_dt = end if end is not None else datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    elapsed_ms = (end_dt - start_dt).total_seconds() * 1000.0
    if elapsed_ms < 0:
        logger.warning(
            "SentinelGrid metrics: start timestamp %r is after the end time (clock skew or a "
            "future telemetry timestamp); skipping latency calculation rather than publishing "
            "a misleading negative value.",
            start_iso,
        )
        return None

    return elapsed_ms


def _build_dimensions(subsystem: Optional[str], fault_type: Optional[str]) -> Optional[List[dict]]:
    """Builds the `[{"Name": .., "Value": ..}, ...]` shape `put_metric_data`
    needs. Returns `None` if either value is missing/empty — CloudWatch
    dimension values cannot be null or empty, and callers must treat a
    `None` return as "cannot safely publish this metric," not invent a
    placeholder value.
    """
    if not subsystem or not fault_type:
        return None
    return [
        {"Name": DIMENSION_SUBSYSTEM, "Value": subsystem},
        {"Name": DIMENSION_FAULT_TYPE, "Value": fault_type},
    ]


class MetricsClient:
    """Best-effort CloudWatch custom-metric publisher.

    Accepts an optional pre-built CloudWatch client (`client=`) for
    dependency injection — tests pass a fake/mock here, so no real AWS
    call is ever made during the test suite, mirroring
    `RemediationTriggerClient`'s/`AnomalyEventStore`'s own `client=`/
    `table=` injection pattern. Production code lets it build its own
    client lazily on first use: constructing a `MetricsClient()` never
    touches AWS by itself, and never requires AWS credentials merely to
    import this module or construct this class.

    Every `put_*` method below returns `True`/`False` (published or not)
    and NEVER raises — see this module's docstring "Best-effort, not
    fail-fast" for why this is the one deliberate exception to this
    project's usual AWS-adapter fail-fast convention.
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

    def _put(
        self,
        metric_name: str,
        value: float,
        unit: str,
        subsystem: Optional[str],
        fault_type: Optional[str],
    ) -> bool:
        dimensions = _build_dimensions(subsystem, fault_type)
        if dimensions is None:
            logger.warning(
                "SentinelGrid metrics: skipping metric_name=%s -- missing subsystem/fault_type "
                "dimension value (subsystem=%r, fault_type=%r).",
                metric_name,
                subsystem,
                fault_type,
            )
            return False

        try:
            client = self._get_client()
            client.put_metric_data(
                Namespace=self.namespace,
                MetricData=[
                    {
                        "MetricName": metric_name,
                        "Dimensions": dimensions,
                        "Value": value,
                        "Unit": unit,
                    }
                ],
            )
            return True
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see module docstring: best-effort, never raises
            logger.warning(
                "SentinelGrid metrics: failed to publish metric_name=%s subsystem=%s fault_type=%s error_type=%s",
                metric_name,
                subsystem,
                fault_type,
                type(exc).__name__,
            )
            return False

    def put_count(
        self,
        metric_name: str,
        subsystem: Optional[str],
        fault_type: Optional[str],
        value: float = 1.0,
    ) -> bool:
        """Publishes a Count metric. Used for every metric in `MetricName`
        except `DETECTION_LATENCY`/`RECOVERY_LATENCY`."""
        return self._put(metric_name, value, Unit.COUNT, subsystem, fault_type)

    def put_milliseconds(
        self,
        metric_name: str,
        subsystem: Optional[str],
        fault_type: Optional[str],
        value: float,
    ) -> bool:
        """Publishes a Milliseconds metric (`DETECTION_LATENCY`/`RECOVERY_LATENCY`)."""
        return self._put(metric_name, value, Unit.MILLISECONDS, subsystem, fault_type)
