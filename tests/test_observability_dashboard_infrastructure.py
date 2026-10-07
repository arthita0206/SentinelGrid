"""Structural tests for the CloudWatch observability dashboard
(infrastructure/observability-dashboard.yaml).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no CloudWatch access, no
credentials. `DashboardBody` is parsed as real JSON (not just
string-matched), and every widget's structure is inspected directly.
"""

import json
import re
from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "observability-dashboard.yaml"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-observability-dashboard.sh"

EXPECTED_METRICS = {
    "FaultDetectionCount",
    "DetectionLatency",
    "RecoveryAttemptCount",
    "RecoverySuccessCount",
    "RecoveryFailureCount",
    "RecoveryLatency",
    "UnresolvedFaultCount",
}

# Prior test node IDs that must never appear hardcoded in a dashboard
# meant to stay useful for future telemetry (see Step 30 instructions).
PRIOR_TEST_NODE_IDS = (
    "power-step23-e2e-001",
    "power-step27-sqs-e2e-20260914132444",
    "power-step29-metrics-e2e-20260914162150",
    "power-stepfunctions-live-001",
    "power-test-persistence-001",
    "power-test-logging-001",
)


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _dashboard_resource(template: dict) -> dict:
    resources = [r for r in template["Resources"].values() if r["Type"] == "AWS::CloudWatch::Dashboard"]
    assert len(resources) == 1, f"expected exactly one AWS::CloudWatch::Dashboard, found {len(resources)}"
    return resources[0]


def _dashboard_body(template: dict) -> dict:
    resource = _dashboard_resource(template)
    body_str = resource["Properties"]["DashboardBody"]
    assert isinstance(body_str, str), "DashboardBody must resolve to a plain string (no unresolved intrinsics)"
    return json.loads(body_str)  # must not raise -- real JSON parsing, not string matching


def _all_widgets(body: dict) -> list:
    assert "widgets" in body
    assert isinstance(body["widgets"], list)
    return body["widgets"]


def _metric_widgets(body: dict) -> list:
    return [w for w in _all_widgets(body) if w.get("type") == "metric"]


def _text_widgets(body: dict) -> list:
    return [w for w in _all_widgets(body) if w.get("type") == "text"]


def _flatten_metric_entries(widget: dict) -> list:
    """Returns every entry in a metric widget's `properties.metrics` array
    (each entry is either a plain metric array or a single-element list
    containing an expression dict), flattened into one list of entries.
    """
    metrics = widget.get("properties", {}).get("metrics", [])
    return metrics


def _referenced_metric_names(body: dict) -> set:
    """Collects every SentinelGrid metric name referenced anywhere in the
    dashboard — both plain `["SentinelGrid", "MetricName", ...]` arrays
    and `SEARCH`/Metrics-Insights `expression` strings that name a metric
    (e.g. `MetricName="X"` or `SELECT SUM(X) FROM ...`).
    """
    names = set()
    for widget in _metric_widgets(body):
        for entry in _flatten_metric_entries(widget):
            if isinstance(entry, list):
                if entry and entry[0] == "SentinelGrid" and len(entry) > 1 and isinstance(entry[1], str):
                    names.add(entry[1])
                # A single-element list can itself contain one expression dict.
                for item in entry:
                    if isinstance(item, dict) and "expression" in item:
                        names.update(_metric_names_from_expression(item["expression"]))
            elif isinstance(entry, dict) and "expression" in entry:
                names.update(_metric_names_from_expression(entry["expression"]))
    return names


def _metric_names_from_expression(expression: str) -> set:
    found = set()
    for metric_name in EXPECTED_METRICS:
        if metric_name in expression:
            found.add(metric_name)
    return found


# --- A. Template exists ---


def test_template_file_exists():
    assert TEMPLATE_PATH.exists()


# --- B. AWSTemplateFormatVersion valid ---


def test_template_parses_and_has_valid_format_version():
    template = _load_template()
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Resources" in template


# --- C, D. Dashboard resource exists, correct type ---


def test_dashboard_resource_exists_with_correct_type():
    template = _load_template()
    resource = _dashboard_resource(template)
    assert resource["Type"] == "AWS::CloudWatch::Dashboard"


def test_only_one_resource_in_the_whole_template():
    template = _load_template()
    assert len(template["Resources"]) == 1


# --- E. DashboardName is SentinelGrid-Observability ---


def test_dashboard_name_is_correct():
    template = _load_template()
    resource = _dashboard_resource(template)
    assert resource["Properties"]["DashboardName"] == "SentinelGrid-Observability"


# --- F. DashboardBody is valid JSON ---


def test_dashboard_body_is_valid_json_with_widgets():
    template = _load_template()
    body = _dashboard_body(template)
    widgets = _all_widgets(body)
    assert len(widgets) > 0


def test_dashboard_start_time_is_relative_not_hardcoded():
    template = _load_template()
    body = _dashboard_body(template)
    assert body.get("start") == "-PT6H"
    # Never a hardcoded absolute date/time -- must remain useful for
    # future telemetry, not pinned to when this step was authored.
    assert "end" not in body or not re.search(r"\d{4}-\d{2}-\d{2}", str(body.get("end", "")))


# --- G. Namespace SentinelGrid present ---


def test_namespace_sentinelgrid_used_in_every_metric_widget():
    template = _load_template()
    body = _dashboard_body(template)
    metric_widgets = _metric_widgets(body)
    assert len(metric_widgets) > 0
    for widget in metric_widgets:
        raw = json.dumps(widget)
        assert "SentinelGrid" in raw, f"widget {widget.get('properties', {}).get('title')!r} does not reference the SentinelGrid namespace"


# --- H-N. Each of the 7 metrics is present ---


def test_all_seven_metrics_are_referenced_somewhere_in_the_dashboard():
    template = _load_template()
    body = _dashboard_body(template)
    referenced = _referenced_metric_names(body)
    missing = EXPECTED_METRICS - referenced
    assert not missing, f"missing metric(s) in dashboard: {missing}"


def test_fault_detection_count_present():
    assert "FaultDetectionCount" in _referenced_metric_names(_dashboard_body(_load_template()))


def test_detection_latency_present():
    assert "DetectionLatency" in _referenced_metric_names(_dashboard_body(_load_template()))


def test_recovery_attempt_count_present():
    assert "RecoveryAttemptCount" in _referenced_metric_names(_dashboard_body(_load_template()))


def test_recovery_success_count_present():
    assert "RecoverySuccessCount" in _referenced_metric_names(_dashboard_body(_load_template()))


def test_recovery_failure_count_present():
    assert "RecoveryFailureCount" in _referenced_metric_names(_dashboard_body(_load_template()))


def test_recovery_latency_present():
    assert "RecoveryLatency" in _referenced_metric_names(_dashboard_body(_load_template()))


def test_unresolved_fault_count_present():
    assert "UnresolvedFaultCount" in _referenced_metric_names(_dashboard_body(_load_template()))


# --- O, P. Subsystem / FaultType dimensions used ---


def test_subsystem_dimension_used():
    template = _load_template()
    body = _dashboard_body(template)
    raw = json.dumps(body)
    assert "Subsystem" in raw


def test_faulttype_dimension_used():
    template = _load_template()
    body = _dashboard_body(template)
    raw = json.dumps(body)
    assert "FaultType" in raw


# --- Q. node_id is NOT used as a metric dimension ---


def test_node_id_never_used_as_a_dimension():
    """Checks actual metric-widget structure (dimension name/value pairs
    and query expressions) for node_id -- NOT the dashboard's raw JSON as
    a whole, since the system-overview text widget legitimately
    *documents* the "never node_id" design decision in prose (see
    test_system_overview_mentions... ) without that being a real
    dimension usage.
    """
    template = _load_template()
    body = _dashboard_body(template)
    for widget in _metric_widgets(body):
        for entry in _flatten_metric_entries(widget):
            if isinstance(entry, list):
                # Plain metric arrays alternate [Namespace, MetricName, DimName, DimValue, ...]
                str_items = [item for item in entry if isinstance(item, str)]
                assert "node_id" not in str_items
            elif isinstance(entry, dict) and "expression" in entry:
                assert "node_id" not in entry["expression"]


def test_no_prior_test_node_ids_hardcoded_in_dashboard():
    template = _load_template()
    body = _dashboard_body(template)
    raw = json.dumps(body)
    for node_id in PRIOR_TEST_NODE_IDS:
        assert node_id not in raw, f"dashboard must not hardcode historical test node_id {node_id!r}"


# --- R. No alarms created ---


def test_no_alarm_resources_created():
    template = _load_template()
    types = {r["Type"] for r in template["Resources"].values()}
    assert "AWS::CloudWatch::Alarm" not in types
    raw = TEMPLATE_PATH.read_text()
    assert "AWS::CloudWatch::Alarm" not in raw


# --- S. No SNS resources created ---


def test_no_sns_resources_created():
    template = _load_template()
    types = {r["Type"] for r in template["Resources"].values()}
    assert not any(t.startswith("AWS::SNS::") for t in types)
    raw = TEMPLATE_PATH.read_text()
    assert "AWS::SNS::" not in raw


# --- T. No Lambda/DynamoDB/SQS resources created by this stack ---


def test_no_lambda_dynamodb_sqs_or_iam_resources_created():
    template = _load_template()
    types = {r["Type"] for r in template["Resources"].values()}
    forbidden_prefixes = ("AWS::Lambda::", "AWS::DynamoDB::", "AWS::SQS::", "AWS::IAM::", "AWS::IoT::", "AWS::StepFunctions::")
    for resource_type in types:
        assert not resource_type.startswith(forbidden_prefixes), f"unexpected resource type {resource_type!r}"


# --- U. No fabricated accuracy/false-positive metrics ---


def test_no_fabricated_accuracy_or_false_positive_metrics():
    template = _load_template()
    raw = TEMPLATE_PATH.read_text()
    forbidden_terms = ("DetectionAccuracy", "FalsePositiveRate", "SystemHealth", "SubsystemHealth", "NodeHealth")
    for term in forbidden_terms:
        assert term not in raw, f"dashboard must not fabricate a {term!r} metric"
    # No literal "95%" (the SRS target) presented as if measured.
    assert "95%" not in raw


def test_dashboard_documents_accuracy_limitation_as_text_not_a_number():
    template = _load_template()
    body = _dashboard_body(template)
    combined_text = " ".join(w["properties"]["markdown"] for w in _text_widgets(body))
    assert "accuracy" in combined_text.lower()
    assert "false-positive" in combined_text.lower() or "false positive" in combined_text.lower()
    assert "ground-truth" in combined_text.lower() or "ground truth" in combined_text.lower()


# --- V. RecoveryLatency not mislabeled MTTR ---


def test_recovery_latency_not_labeled_mttr_in_widget_titles():
    """A title may mention MTTR only as an explicit disclaimer (e.g.
    "... not MTTR") -- never presenting RecoveryLatency AS MTTR (e.g. a
    bare "MTTR" title, or "MTTR" without a negation nearby).
    """
    template = _load_template()
    body = _dashboard_body(template)
    for widget in _metric_widgets(body):
        title = widget.get("properties", {}).get("title", "")
        if "RecoveryLatency" in json.dumps(widget) and "MTTR" in title.upper():
            assert "not mttr" in title.lower() or "not an mttr" in title.lower()


def test_mttr_caveat_present_in_dashboard_text():
    template = _load_template()
    body = _dashboard_body(template)
    combined_text = " ".join(w["properties"]["markdown"] for w in _text_widgets(body))
    assert "MTTR" in combined_text
    assert "RecoveryLatency" in combined_text or "recovery latency" in combined_text.lower()


def test_no_mttr_label_anywhere_claiming_it_is_calculated():
    """MTTR may be mentioned only in the explicit caveat disclaiming it --
    never as a widget title/label presenting RecoveryLatency AS MTTR."""
    template = _load_template()
    body = _dashboard_body(template)
    for widget in _all_widgets(body):
        title = widget.get("properties", {}).get("title", "")
        assert title.strip().upper() != "MTTR"
        assert "MTTR" not in title or "not" in title.lower()


# --- W. Dashboard has the system overview text ---


def test_system_overview_text_widget_present():
    template = _load_template()
    body = _dashboard_body(template)
    text_widgets = _text_widgets(body)
    assert any("System Overview" in w["properties"]["markdown"] for w in text_widgets)


def test_system_overview_mentions_the_architectural_chain():
    template = _load_template()
    body = _dashboard_body(template)
    text_widgets = _text_widgets(body)
    overview = next(w["properties"]["markdown"] for w in text_widgets if "System Overview" in w["properties"]["markdown"])
    for term in ("IoT", "SQS", "Analyzer", "Remediation"):
        assert term in overview


def test_system_overview_contains_no_secrets_or_account_ids():
    template = _load_template()
    body = _dashboard_body(template)
    raw = json.dumps(body)
    assert "401429382000" not in raw  # no hardcoded account ID
    assert "arn:aws" not in raw  # no embedded ARNs
    secret_pattern = re.compile(r"AKIA[0-9A-Z]{16}|BEGIN[ A-Z]*PRIVATE KEY|aws_secret_access_key", re.IGNORECASE)
    assert not secret_pattern.search(raw)


# --- X. Dashboard has the subsystem-oriented view ---


def test_subsystem_oriented_view_present():
    template = _load_template()
    body = _dashboard_body(template)
    metric_widgets = _metric_widgets(body)
    subsystem_titled = [w for w in metric_widgets if "subsystem" in w["properties"].get("title", "").lower()]
    assert len(subsystem_titled) >= 1


def test_no_per_node_widgets():
    template = _load_template()
    body = _dashboard_body(template)
    for widget in _all_widgets(body):
        title = widget.get("properties", {}).get("title", "")
        assert "node-" not in title.lower() and "power-" not in title.lower()


# --- Widget structural validation (Section 14) ---


def test_every_widget_has_required_structural_fields():
    template = _load_template()
    body = _dashboard_body(template)
    for widget in _all_widgets(body):
        assert widget["type"] in ("text", "metric")
        for field in ("x", "y", "width", "height", "properties"):
            assert field in widget, f"widget missing {field!r}: {widget}"
        assert isinstance(widget["x"], int) and widget["x"] >= 0
        assert isinstance(widget["y"], int) and widget["y"] >= 0
        assert isinstance(widget["width"], int) and 0 < widget["width"] <= 24
        assert isinstance(widget["height"], int) and widget["height"] > 0


def test_no_widget_extends_beyond_the_24_column_grid():
    template = _load_template()
    body = _dashboard_body(template)
    for widget in _all_widgets(body):
        assert widget["x"] + widget["width"] <= 24, f"widget overflows grid: {widget}"


def test_metric_widgets_have_valid_metrics_arrays():
    template = _load_template()
    body = _dashboard_body(template)
    for widget in _metric_widgets(body):
        metrics = widget["properties"].get("metrics")
        assert isinstance(metrics, list) and len(metrics) > 0
        for entry in metrics:
            assert isinstance(entry, list) and len(entry) > 0
            if isinstance(entry[0], str):
                # Plain metric array: [Namespace, MetricName, DimName, DimValue, ...(, options dict)]
                assert entry[0] == "SentinelGrid"
                assert isinstance(entry[1], str)
                dims_and_rest = entry[2:]
                # Trailing options dict (if present) is not a dimension pair.
                if dims_and_rest and isinstance(dims_and_rest[-1], dict):
                    dims_and_rest = dims_and_rest[:-1]
                assert len(dims_and_rest) % 2 == 0, f"dimension name/value pairs must be even: {entry}"
            else:
                # Expression-based entry: a single dict with "expression" and "id".
                assert isinstance(entry[0], dict)
                assert "expression" in entry[0]
                assert "id" in entry[0]


def test_text_widgets_have_non_empty_markdown():
    template = _load_template()
    body = _dashboard_body(template)
    for widget in _text_widgets(body):
        markdown = widget["properties"].get("markdown")
        assert isinstance(markdown, str) and len(markdown.strip()) > 0


# --- Statistics/period per Step 30 instructions ---


def test_fault_detection_count_widget_uses_sum_statistic_and_300s_period():
    template = _load_template()
    body = _dashboard_body(template)
    widget = next(
        w for w in _metric_widgets(body) if w["properties"].get("title") == "Fault Detection Count (power / NODE_OVERLOAD)"
    )
    assert widget["properties"]["stat"] == "Sum"
    assert widget["properties"]["period"] == 300


def test_detection_latency_widget_uses_average_statistic_and_300s_period():
    template = _load_template()
    body = _dashboard_body(template)
    widget = next(w for w in _metric_widgets(body) if w["properties"].get("title") == "Detection Latency (Milliseconds)")
    assert widget["properties"]["stat"] == "Average"
    assert widget["properties"]["period"] == 300


def test_recovery_outcomes_widget_uses_sum_statistic_and_contains_three_metrics():
    template = _load_template()
    body = _dashboard_body(template)
    widget = next(w for w in _metric_widgets(body) if w["properties"].get("title") == "Recovery Outcomes")
    assert widget["properties"]["stat"] == "Sum"
    assert widget["properties"]["period"] == 300
    assert len(widget["properties"]["metrics"]) == 3


def test_unresolved_fault_count_widget_uses_sum_statistic():
    template = _load_template()
    body = _dashboard_body(template)
    widget = next(w for w in _metric_widgets(body) if w["properties"].get("title") == "Unresolved / Escalated Faults")
    assert widget["properties"]["stat"] == "Sum"
    assert widget["properties"]["period"] == 300


# --- Deploy script conventions ---


def test_deploy_script_exists():
    assert DEPLOY_SCRIPT.exists()


def test_deploy_script_uses_correct_stack_name_profile_and_region():
    script = DEPLOY_SCRIPT.read_text()
    assert 'STACK_NAME="${STACK_NAME:-sentinelgrid-observability-dashboard}"' in script
    assert 'AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"' in script
    assert 'AWS_REGION="${AWS_REGION:-ap-south-1}"' in script


def test_deploy_script_does_not_touch_any_other_stack():
    script = DEPLOY_SCRIPT.read_text()
    assert script.count("cloudformation deploy") == 1
    for other_stack in (
        "sentinelgrid-telemetry-sqs-queue",
        "sentinelgrid-sqs-consumer",
        "sentinelgrid-anomaly-detector",
        "sentinelgrid-anomaly-detection-rule",
        "sentinelgrid-telemetry-storage",
        "sentinelgrid-anomaly-events-storage",
        "sentinelgrid-remediation-trigger",
        "sentinelgrid-remediation-workflow",
        "sentinelgrid-remediation-lambdas",
    ):
        assert other_stack not in script


def test_deploy_script_does_not_pass_capabilities_flag():
    """No IAM resources are created by this template, so no
    --capabilities flag should be needed or present."""
    script = DEPLOY_SCRIPT.read_text()
    assert "CAPABILITY_" not in script


def test_top_level_description_within_limit():
    template = _load_template()
    assert len(template["Description"]) <= 1024


def test_no_hardcoded_account_specific_arn_in_template():
    raw = TEMPLATE_PATH.read_text()
    assert not re.search(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:", raw)
