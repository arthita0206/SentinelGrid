"""Structural tests for the CloudWatch alarm + SNS escalation
infrastructure (infrastructure/observability-alerting.yaml).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no CloudWatch/SNS access, no
credentials.
"""

import hashlib
import re
from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "observability-alerting.yaml"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-observability-alerting.sh"
DASHBOARD_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "observability-dashboard.yaml"

# Captured before this step touched anything -- the dashboard must
# remain byte-for-byte unchanged unless a strictly necessary, documented
# reason forced an edit (Step 31 instructions: "prefer no dashboard
# changes").
DASHBOARD_SHA256_BEFORE_STEP_31 = "4458006ee269ee12c0df0056a027bb52b1662b97d538de67a351288fccb4b64e"


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _resources_of_type(template: dict, resource_type: str) -> list:
    return [r for r in template["Resources"].values() if r["Type"] == resource_type]


def _topic(template: dict) -> dict:
    topics = _resources_of_type(template, "AWS::SNS::Topic")
    assert len(topics) == 1, f"expected exactly one AWS::SNS::Topic, found {len(topics)}"
    return topics[0]


def _topic_logical_id(template: dict) -> str:
    for logical_id, resource in template["Resources"].items():
        if resource["Type"] == "AWS::SNS::Topic":
            return logical_id
    raise AssertionError("no AWS::SNS::Topic resource found")


def _alarm(template: dict) -> dict:
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == 1, f"expected exactly one AWS::CloudWatch::Alarm, found {len(alarms)}"
    return alarms[0]


# --- A. Template exists ---


def test_template_file_exists():
    assert TEMPLATE_PATH.exists()


def test_template_parses_and_has_valid_format_version():
    template = _load_template()
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Resources" in template


# --- B, C. SNS topic exists, correct type ---


def test_sns_topic_exists_with_correct_type():
    template = _load_template()
    topic = _topic(template)
    assert topic["Type"] == "AWS::SNS::Topic"


# --- D. Topic name ---


def test_topic_name_is_correct():
    template = _load_template()
    topic = _topic(template)
    assert topic["Properties"]["TopicName"] == "SentinelGrid-UnresolvedFaults"


# --- E. No SNS subscription resource ---


def test_no_sns_subscription_resource_created():
    template = _load_template()
    assert _resources_of_type(template, "AWS::SNS::Subscription") == []
    raw = TEMPLATE_PATH.read_text()
    assert "AWS::SNS::Subscription" not in raw


def test_no_sns_topic_policy_resource_created():
    """CloudWatch Alarm -> SNS in the same account works via built-in
    service trust; no explicit topic policy resource should be needed or
    present (see Step 31 instructions section 8)."""
    template = _load_template()
    assert _resources_of_type(template, "AWS::SNS::TopicPolicy") == []


def test_topic_has_no_hardcoded_endpoint_or_email():
    raw = TEMPLATE_PATH.read_text()
    assert "@" not in raw  # no email address anywhere in the template
    assert "Endpoint" not in raw
    assert "Protocol" not in raw


# --- F. CloudWatch alarm exists ---


def test_cloudwatch_alarm_exists_with_correct_type():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Type"] == "AWS::CloudWatch::Alarm"


# --- G. Alarm name ---


def test_alarm_name_is_correct():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Properties"]["AlarmName"] == "SentinelGrid-UnresolvedFaults-Alarm"


# --- H. Namespace ---


def test_alarm_namespace_is_sentinelgrid():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Properties"]["Namespace"] == "SentinelGrid"


# --- I. MetricName ---


def test_alarm_metric_name_is_unresolvedfaultcount():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Properties"]["MetricName"] == "UnresolvedFaultCount"


# --- J. Statistic ---


def test_alarm_statistic_is_sum():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Properties"]["Statistic"] == "Sum"


# --- K. Period ---


def test_alarm_period_defaults_to_300_via_parameter():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Properties"]["Period"] == {"Ref": "AlarmPeriodSeconds"}
    params = template["Parameters"]
    assert "AlarmPeriodSeconds" in params
    assert int(params["AlarmPeriodSeconds"]["Default"]) == 300
    assert int(params["AlarmPeriodSeconds"]["MinValue"]) >= 60


# --- L. EvaluationPeriods ---


def test_alarm_evaluation_periods_is_one():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Properties"]["EvaluationPeriods"] == 1


# --- M. Threshold ---


def test_alarm_threshold_defaults_to_one_via_parameter():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Properties"]["Threshold"] == {"Ref": "AlarmThreshold"}
    params = template["Parameters"]
    assert "AlarmThreshold" in params
    assert int(params["AlarmThreshold"]["Default"]) == 1
    assert int(params["AlarmThreshold"]["MinValue"]) >= 1


# --- N. ComparisonOperator ---


def test_alarm_comparison_operator_is_greater_than_or_equal():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Properties"]["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"


# --- O. TreatMissingData ---


def test_alarm_treats_missing_data_as_not_breaching():
    template = _load_template()
    alarm = _alarm(template)
    assert alarm["Properties"]["TreatMissingData"] == "notBreaching"


# --- P. AlarmActions references the SNS topic ---


def test_alarm_actions_references_exactly_the_sns_topic():
    template = _load_template()
    alarm = _alarm(template)
    topic_logical_id = _topic_logical_id(template)
    assert alarm["Properties"]["AlarmActions"] == [{"Ref": topic_logical_id}]


# --- Q, R. No OKActions / InsufficientDataActions ---


def test_no_ok_actions_configured():
    template = _load_template()
    alarm = _alarm(template)
    assert "OKActions" not in alarm["Properties"]


def test_no_insufficient_data_actions_configured():
    template = _load_template()
    alarm = _alarm(template)
    assert "InsufficientDataActions" not in alarm["Properties"]


# --- S. No node_id dimension ---


def test_dimensions_are_exactly_subsystem_and_faulttype_no_node_id():
    template = _load_template()
    alarm = _alarm(template)
    dimensions = alarm["Properties"]["Dimensions"]
    dim_names = {d["Name"] for d in dimensions}
    assert dim_names == {"Subsystem", "FaultType"}
    assert "node_id" not in dim_names


def test_dimension_values_match_the_only_proven_live_combination():
    template = _load_template()
    alarm = _alarm(template)
    dims = {d["Name"]: d["Value"] for d in alarm["Properties"]["Dimensions"]}
    assert dims == {"Subsystem": "power", "FaultType": "NODE_OVERLOAD"}


# --- T-W. No Lambda/DynamoDB/SQS/Step Functions/IAM resources ---


def test_no_lambda_dynamodb_sqs_stepfunctions_or_iam_resources():
    template = _load_template()
    types = {r["Type"] for r in template["Resources"].values()}
    forbidden_prefixes = (
        "AWS::Lambda::",
        "AWS::DynamoDB::",
        "AWS::SQS::",
        "AWS::StepFunctions::",
        "AWS::IAM::",
        "AWS::IoT::",
    )
    for resource_type in types:
        assert not resource_type.startswith(forbidden_prefixes), f"unexpected resource type {resource_type!r}"


# --- Y. No additional alarms ---


def test_exactly_two_resources_in_the_whole_template():
    template = _load_template()
    assert len(template["Resources"]) == 2
    assert len(_resources_of_type(template, "AWS::CloudWatch::Alarm")) == 1


def test_no_other_metric_alarms_for_success_or_failure_counts():
    raw = TEMPLATE_PATH.read_text()
    for forbidden_metric in (
        "RecoverySuccessCount",
        "RecoveryAttemptCount",
        "RecoveryFailureCount",
        "FaultDetectionCount",
        "DetectionLatency",
        "RecoveryLatency",
    ):
        assert forbidden_metric not in raw, f"Step 31 must only alarm on UnresolvedFaultCount, found {forbidden_metric!r}"


# --- Z. No fabricated metrics ---


def test_no_fabricated_metric_names():
    raw = TEMPLATE_PATH.read_text()
    for fabricated in ("SystemHealth", "SubsystemHealth", "NodeHealth", "MTTR", "DetectionAccuracy", "FalsePositiveRate"):
        assert fabricated not in raw


# --- AA. Alarm description explains event/count semantics ---


def test_alarm_description_explains_event_count_semantics():
    template = _load_template()
    alarm = _alarm(template)
    description = alarm["Properties"]["AlarmDescription"].lower()
    assert "unresolved" in description
    assert "observation" in description or "event" in description or "count" in description
    assert len(alarm["Properties"]["AlarmDescription"]) <= 1024


# --- AB. Dashboard is not unnecessarily modified ---


def test_dashboard_template_unchanged_by_step_31():
    digest = hashlib.sha256(DASHBOARD_TEMPLATE_PATH.read_bytes()).hexdigest()
    assert digest == DASHBOARD_SHA256_BEFORE_STEP_31, (
        "infrastructure/observability-dashboard.yaml changed during Step 31 -- "
        "instructions say to avoid this unless strictly necessary and documented"
    )


# --- No hardcoded account-specific ARNs ---


def test_no_hardcoded_account_specific_arn_in_template():
    raw = TEMPLATE_PATH.read_text()
    assert not re.search(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:", raw)


def test_top_level_description_within_limit():
    template = _load_template()
    assert len(template["Description"]) <= 1024


# ============================================================================
# DEPLOY SCRIPT CONVENTIONS
# ============================================================================


def test_deploy_script_exists():
    assert DEPLOY_SCRIPT.exists()


def test_deploy_script_uses_correct_stack_name_profile_and_region():
    script = DEPLOY_SCRIPT.read_text()
    assert 'STACK_NAME="${STACK_NAME:-sentinelgrid-observability-alerting}"' in script
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
        "sentinelgrid-observability-dashboard",
    ):
        assert other_stack not in script


def test_deploy_script_does_not_pass_capabilities_flag():
    """No IAM resources are created by this template, so no
    --capabilities flag should be needed or present."""
    script = DEPLOY_SCRIPT.read_text()
    assert "CAPABILITY_" not in script


def test_deploy_script_does_not_create_a_subscription():
    """The script's trailing NOTE documents a MANUAL follow-up example
    (`aws sns subscribe ...`) via `echo` -- it must never be an actual,
    executed invocation of that command.
    """
    script = DEPLOY_SCRIPT.read_text()
    for line in script.splitlines():
        stripped = line.strip()
        if "aws sns subscribe" in stripped:
            assert stripped.startswith("echo"), f"deploy script must not actually create a subscription: {line!r}"
