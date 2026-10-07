"""Unit tests for shared/metrics.py (Step 28, SRS FR-7.1's observability
foundation): the CloudWatch metric contract and best-effort publisher.

These tests never touch AWS — `MetricsClient` is always constructed with
an injected fake/mock `client`, exactly mirroring
`RemediationTriggerClient`'s own test pattern (see
tests/test_remediation_trigger_client.py). No AWS credentials, boto3
clients, or network access are required.
"""

import ast
import hashlib
import inspect
import pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from shared.metrics import (
    DIMENSION_FAULT_TYPE,
    DIMENSION_SUBSYSTEM,
    NAMESPACE,
    MetricName,
    MetricsClient,
    Unit,
    compute_elapsed_ms,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# --- A. Correct namespace ---


def test_default_namespace_is_sentinelgrid():
    assert NAMESPACE == "SentinelGrid"
    client = MetricsClient(client=MagicMock())
    assert client.namespace == "SentinelGrid"


def test_namespace_used_in_put_metric_data_call():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    client.put_count(MetricName.FAULT_DETECTION_COUNT, "power", "NODE_OVERLOAD")
    assert mock_client.put_metric_data.call_args.kwargs["Namespace"] == "SentinelGrid"


# --- B. Correct metric names ---


def test_metric_name_constants():
    assert MetricName.FAULT_DETECTION_COUNT == "FaultDetectionCount"
    assert MetricName.RECOVERY_ATTEMPT_COUNT == "RecoveryAttemptCount"
    assert MetricName.RECOVERY_SUCCESS_COUNT == "RecoverySuccessCount"
    assert MetricName.RECOVERY_FAILURE_COUNT == "RecoveryFailureCount"
    assert MetricName.UNRESOLVED_FAULT_COUNT == "UnresolvedFaultCount"
    assert MetricName.DETECTION_LATENCY == "DetectionLatency"
    assert MetricName.RECOVERY_LATENCY == "RecoveryLatency"


def test_put_metric_data_uses_the_given_metric_name():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    client.put_count(MetricName.RECOVERY_SUCCESS_COUNT, "water", "SENSOR_DROPOUT")
    metric_datum = mock_client.put_metric_data.call_args.kwargs["MetricData"][0]
    assert metric_datum["MetricName"] == "RecoverySuccessCount"


# --- C. Correct dimensions ---


def test_dimensions_are_subsystem_and_faulttype_only():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    client.put_count(MetricName.FAULT_DETECTION_COUNT, "power", "NODE_OVERLOAD")

    metric_datum = mock_client.put_metric_data.call_args.kwargs["MetricData"][0]
    assert metric_datum["Dimensions"] == [
        {"Name": DIMENSION_SUBSYSTEM, "Value": "power"},
        {"Name": DIMENSION_FAULT_TYPE, "Value": "NODE_OVERLOAD"},
    ]


def test_node_id_cannot_be_passed_as_a_dimension():
    """Structural guarantee (not just a convention): neither publish
    method even accepts a node_id-shaped parameter, so there is no way
    to smuggle a high-cardinality dimension through this client.
    """
    sig = inspect.signature(MetricsClient.put_count)
    assert "node_id" not in sig.parameters
    sig2 = inspect.signature(MetricsClient.put_milliseconds)
    assert "node_id" not in sig2.parameters


def test_missing_subsystem_skips_publication_without_raising():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    result = client.put_count(MetricName.FAULT_DETECTION_COUNT, None, "NODE_OVERLOAD")
    assert result is False
    mock_client.put_metric_data.assert_not_called()


def test_missing_fault_type_skips_publication_without_raising():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    result = client.put_count(MetricName.FAULT_DETECTION_COUNT, "power", None)
    assert result is False
    mock_client.put_metric_data.assert_not_called()


def test_empty_string_subsystem_skips_publication():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    result = client.put_count(MetricName.FAULT_DETECTION_COUNT, "", "NODE_OVERLOAD")
    assert result is False
    mock_client.put_metric_data.assert_not_called()


# --- D. Correct units ---


def test_put_count_uses_count_unit():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    client.put_count(MetricName.RECOVERY_ATTEMPT_COUNT, "power", "NODE_OVERLOAD")
    metric_datum = mock_client.put_metric_data.call_args.kwargs["MetricData"][0]
    assert metric_datum["Unit"] == Unit.COUNT == "Count"


def test_put_milliseconds_uses_milliseconds_unit():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    client.put_milliseconds(MetricName.DETECTION_LATENCY, "power", "NODE_OVERLOAD", 123.4)
    metric_datum = mock_client.put_metric_data.call_args.kwargs["MetricData"][0]
    assert metric_datum["Unit"] == Unit.MILLISECONDS == "Milliseconds"


# --- E. Correct values ---


def test_put_count_default_value_is_one():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    client.put_count(MetricName.FAULT_DETECTION_COUNT, "power", "NODE_OVERLOAD")
    metric_datum = mock_client.put_metric_data.call_args.kwargs["MetricData"][0]
    assert metric_datum["Value"] == 1.0


def test_put_count_accepts_an_explicit_value():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    client.put_count(MetricName.FAULT_DETECTION_COUNT, "power", "NODE_OVERLOAD", value=3.0)
    metric_datum = mock_client.put_metric_data.call_args.kwargs["MetricData"][0]
    assert metric_datum["Value"] == 3.0


def test_put_milliseconds_uses_given_value():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    client.put_milliseconds(MetricName.RECOVERY_LATENCY, "power", "NODE_OVERLOAD", 4567.8)
    metric_datum = mock_client.put_metric_data.call_args.kwargs["MetricData"][0]
    assert metric_datum["Value"] == 4567.8


# --- Metric publication is best-effort: CloudWatch failures never raise ---


def test_put_metric_data_exception_is_swallowed_and_returns_false():
    mock_client = MagicMock()
    mock_client.put_metric_data.side_effect = RuntimeError("simulated CloudWatch outage")
    client = MetricsClient(client=mock_client)
    result = client.put_count(MetricName.FAULT_DETECTION_COUNT, "power", "NODE_OVERLOAD")
    assert result is False  # never raises


def test_successful_publish_returns_true():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    assert client.put_count(MetricName.FAULT_DETECTION_COUNT, "power", "NODE_OVERLOAD") is True


# --- Q. Metric client is mockable without AWS credentials ---


def test_constructing_client_never_touches_aws():
    # No client= injected, no credentials configured anywhere in this
    # sandbox -- construction alone must not raise or attempt any
    # network call.
    client = MetricsClient()
    assert client.namespace == "SentinelGrid"


def test_injected_client_is_used_instead_of_a_real_one():
    mock_client = MagicMock()
    client = MetricsClient(client=mock_client)
    assert client._get_client() is mock_client


# --- DetectionLatency/RecoveryLatency calculation (compute_elapsed_ms) ---


def test_compute_elapsed_ms_normal_case():
    start = "2026-01-01T00:00:00+00:00"
    end = datetime(2026, 1, 1, 0, 0, 1, 500000, tzinfo=timezone.utc)
    elapsed = compute_elapsed_ms(start, end)
    assert elapsed == pytest.approx(1500.0)


def test_compute_elapsed_ms_missing_timestamp_returns_none():
    assert compute_elapsed_ms(None) is None
    assert compute_elapsed_ms("") is None


# --- O. Invalid telemetry timestamp does not crash detection ---


def test_compute_elapsed_ms_malformed_timestamp_returns_none_and_does_not_raise():
    assert compute_elapsed_ms("not-a-timestamp") is None
    assert compute_elapsed_ms("2026-13-99T99:99:99") is None
    assert compute_elapsed_ms(12345) is None  # wrong type entirely -- must not raise


# --- P. Future timestamp is handled safely ---


def test_compute_elapsed_ms_future_timestamp_returns_none_safely():
    end = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    future_start = "2026-01-01T00:05:00+00:00"  # 5 minutes AFTER end
    assert compute_elapsed_ms(future_start, end) is None


def test_compute_elapsed_ms_naive_timestamp_is_treated_as_utc():
    start = "2026-01-01T00:00:00"  # no UTC offset
    end = datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
    elapsed = compute_elapsed_ms(start, end)
    assert elapsed == pytest.approx(2000.0)


def test_compute_elapsed_ms_defaults_end_to_now():
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    elapsed = compute_elapsed_ms(past)
    assert elapsed is not None
    assert elapsed >= 900  # roughly 1 second, allowing scheduling slack


# --- S. Deterministic detector/engine remain AWS-independent ---


def _module_imports(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "relative_path",
    [
        "lambda_functions/anomaly_detector/detector.py",
        "remediation/models.py",
        "remediation/engine.py",
    ],
)
def test_deterministic_modules_have_no_boto3_or_metrics_import(relative_path):
    imports = _module_imports(REPO_ROOT / relative_path)
    assert "boto3" not in imports
    assert "botocore" not in imports
    assert "shared.metrics" not in imports
    assert "shared" not in imports


# --- T. Existing Step Functions JSON remains unchanged ---


def test_step_functions_asl_file_unchanged_by_step_28():
    asl_path = REPO_ROOT / "step_functions" / "remediation_workflow.json"
    digest = hashlib.sha256(asl_path.read_bytes()).hexdigest()
    # Captured before any Step 28 code change was made. Step 28 is
    # explicitly forbidden from modifying states/transitions/ResultPaths/
    # InputPaths/Catch blocks/Retry behavior -- this guards the whole
    # file byte-for-byte, not just those fields.
    #
    # STEP 51 UPDATE: this hash legitimately changed in Step 51, which
    # (unlike Step 28) was explicitly chartered to modify this exact file
    # to implement FR-5.4's configurable retry/escalation behavior — see
    # docs/step51_remediation_retry.md for the full before/after hash
    # record and design rationale. The guard's original intent (Step 28
    # itself never touched this file) remains true and is preserved by
    # updating the expected digest to the post-Step-51 value rather than
    # deleting this test.
    assert digest == "d1607fa3840011d4f84c8d4c009c8a270f5fe24f5030fa3e720150b113a5e516"
