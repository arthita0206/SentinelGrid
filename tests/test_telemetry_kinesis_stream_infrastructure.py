"""Structural tests for the Kinesis telemetry ingestion infrastructure
(infrastructure/telemetry-kinesis-stream.yaml) and its deployment script.

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3/kinesis client, no network access, no
credentials. They validate local YAML structure only.
"""

import re
from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "telemetry-kinesis-stream.yaml"
DEPLOY_SCRIPT_PATH = REPO_ROOT / "infrastructure" / "deploy-telemetry-kinesis-stream.sh"

EXISTING_TEMPLATES = [
    "telemetry-storage.yaml",
    "anomaly-detector.yaml",
    "anomaly-detection-rule.yaml",
    "anomaly-events-storage.yaml",
    "remediation-workflow.yaml",
    "remediation-lambdas.yaml",
    "remediation-trigger.yaml",
]


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _resources_of_type(template: dict, resource_type: str) -> list:
    return [r for r in template["Resources"].values() if r["Type"] == resource_type]


# --- A. Kinesis stream exists in the template ---


def test_template_parses_and_has_exactly_one_kinesis_stream():
    assert TEMPLATE_PATH.exists()
    template = _load_template()
    streams = _resources_of_type(template, "AWS::Kinesis::Stream")
    assert len(streams) == 1


# --- B. Stream name is stable ---


def test_stream_has_stable_physical_name():
    template = _load_template()
    stream = _resources_of_type(template, "AWS::Kinesis::Stream")[0]
    assert stream["Properties"]["Name"] == "SentinelGridTelemetryStream"


def test_stream_uses_on_demand_capacity_not_unnecessary_provisioned_shards():
    template = _load_template()
    stream = _resources_of_type(template, "AWS::Kinesis::Stream")[0]
    props = stream["Properties"]
    assert props["StreamModeDetails"]["StreamMode"] == "ON_DEMAND"
    assert "ShardCount" not in props


def test_stream_has_retain_deletion_policy():
    template = _load_template()
    stream_logical_id = _resources_of_type(template, "AWS::Kinesis::Stream")
    # Re-fetch the raw resource block (not just Properties) for DeletionPolicy.
    raw_resources = template["Resources"]
    stream_resource = next(r for r in raw_resources.values() if r["Type"] == "AWS::Kinesis::Stream")
    assert stream_resource.get("DeletionPolicy") == "Retain"
    assert stream_resource.get("UpdateReplacePolicy") == "Retain"


# --- C. Retention is configurable and defaults to 24 hours ---


def test_retention_is_a_parameter_defaulting_to_24_hours():
    template = _load_template()
    params = template["Parameters"]
    assert "RetentionPeriodHours" in params
    assert params["RetentionPeriodHours"]["Default"] == 24

    stream = _resources_of_type(template, "AWS::Kinesis::Stream")[0]
    assert stream["Properties"]["RetentionPeriodHours"] == {"Ref": "RetentionPeriodHours"}


def test_retention_is_not_hardcoded_in_application_code():
    # FR-3.2: retention must be CloudFormation configuration, not baked
    # into Python. No production source file should reference a specific
    # retention-hours value.
    for py_file in (REPO_ROOT / "simulator").glob("*.py"):
        content = py_file.read_text()
        assert "RetentionPeriodHours" not in content
        assert "retention_period_hours" not in content.lower()


# --- D. IoT rule consumes sentinelgrid/telemetry ---


def test_rule_consumes_the_existing_telemetry_topic():
    template = _load_template()
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")[0]
    sql = rule["Properties"]["TopicRulePayload"]["Sql"]
    assert "sentinelgrid/telemetry" in sql
    assert rule["Properties"]["TopicRulePayload"]["RuleDisabled"] is False


def test_rule_selects_the_entire_payload_no_schema_transformation():
    template = _load_template()
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")[0]
    sql = rule["Properties"]["TopicRulePayload"]["Sql"]
    assert sql == "SELECT * FROM 'sentinelgrid/telemetry'"


def test_rule_name_is_distinct_from_existing_rules():
    template = _load_template()
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")[0]
    rule_name = rule["Properties"]["RuleName"]
    assert rule_name == "sentinelgrid_telemetry_to_kinesis"
    assert rule_name not in ("sentinelgrid_telemetry_to_dynamodb", "sentinelgrid_telemetry_to_lambda")


# --- E. IoT rule sends telemetry to Kinesis ---


def test_rule_action_is_kinesis_targeting_the_new_stream():
    template = _load_template()
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")[0]
    actions = rule["Properties"]["TopicRulePayload"]["Actions"]
    assert len(actions) == 1
    assert "Kinesis" in actions[0]
    assert actions[0]["Kinesis"]["StreamName"] == {"Ref": "TelemetryStream"}


# --- F. Kinesis partition key is derived from subsystem ---


def test_partition_key_is_derived_from_subsystem_not_node_id():
    template = _load_template()
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")[0]
    action = rule["Properties"]["TopicRulePayload"]["Actions"][0]["Kinesis"]
    assert action["PartitionKey"] == "${subsystem}"
    assert "node_id" not in action["PartitionKey"]


# --- G. No separate streams per subsystem ---


def test_exactly_one_stream_serves_all_subsystems():
    template = _load_template()
    streams = _resources_of_type(template, "AWS::Kinesis::Stream")
    assert len(streams) == 1
    raw = TEMPLATE_PATH.read_text()
    for subsystem in ("power", "water", "traffic"):
        assert f"SentinelGridTelemetryStream{subsystem}" not in raw
        assert f"{subsystem}-stream" not in raw.lower()


# --- H, I, J. IAM least privilege ---


def _role_properties(template: dict) -> dict:
    return _resources_of_type(template, "AWS::IAM::Role")[0]["Properties"]


def test_exactly_one_iam_role_trusting_only_iot():
    template = _load_template()
    roles = _resources_of_type(template, "AWS::IAM::Role")
    assert len(roles) == 1
    statements = roles[0]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    assert statements[0]["Principal"] == {"Service": "iot.amazonaws.com"}


def test_role_grants_exactly_kinesis_putrecord():
    template = _load_template()
    props = _role_properties(template)
    policies = props.get("Policies", [])
    assert len(policies) == 1
    statement = policies[0]["PolicyDocument"]["Statement"][0]
    actions = statement["Action"]
    actions = actions if isinstance(actions, list) else [actions]
    assert actions == ["kinesis:PutRecord"]


def test_iam_resource_is_the_exact_stream_arn_not_wildcard():
    template = _load_template()
    props = _role_properties(template)
    statement = props["Policies"][0]["PolicyDocument"]["Statement"][0]
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert resources == [{"Fn::GetAtt": ["TelemetryStream", "Arn"]}]
    assert "*" not in resources


def test_no_wildcard_kinesis_permission_anywhere():
    raw = TEMPLATE_PATH.read_text()
    assert "kinesis:*" not in raw
    assert '"*"' not in raw
    assert not re.search(r"Resource:\s*\*\s*$", raw, re.MULTILINE)


def test_no_unrelated_permissions_introduced():
    raw = TEMPLATE_PATH.read_text()
    for forbidden_prefix in (
        "lambda:",
        "states:",
        "dynamodb:",
        "iam:",
        "s3:",
        "sns:",
        "sqs:",
        "ec2:",
        "cloudformation:",
    ):
        assert forbidden_prefix not in raw, f"found forbidden permission prefix {forbidden_prefix!r}"
    assert "AdministratorAccess" not in raw
    assert "PowerUserAccess" not in raw


# --- L. Encryption configuration ---


def test_stream_encryption_uses_aws_managed_kms_key():
    template = _load_template()
    stream = _resources_of_type(template, "AWS::Kinesis::Stream")[0]
    encryption = stream["Properties"]["StreamEncryption"]
    assert encryption["EncryptionType"] == "KMS"
    assert encryption["KeyId"] == "alias/aws/kinesis"


def test_no_customer_managed_kms_key_resource_created():
    template = _load_template()
    assert _resources_of_type(template, "AWS::KMS::Key") == []


# --- Only expected resource types; no unrelated infra referenced/modified ---


def test_only_expected_resource_types_are_created():
    template = _load_template()
    actual_types = {r["Type"] for r in template["Resources"].values()}
    assert actual_types == {"AWS::Kinesis::Stream", "AWS::IAM::Role", "AWS::IoT::TopicRule"}


def test_template_does_not_reference_unrelated_existing_infrastructure():
    raw = TEMPLATE_PATH.read_text()
    for name in (
        "SentinelGridAnomalyDetector",
        "SentinelGridAnomalyEvents",
        "sentinelgrid_telemetry_to_lambda",
        "sentinelgrid_telemetry_to_dynamodb",
        "SentinelGridRemediationPlanner",
        "SentinelGridRemediationExecutor",
        "SentinelGridRemediationVerifier",
        "SentinelGridRemediationTrigger",
        "SentinelGridRemediationWorkflow",
    ):
        assert name not in raw, f"unexpectedly references {name!r}"

    # The DynamoDB telemetry table's exact name ("SentinelGridTelemetry")
    # must not appear as a standalone reference — only as a prefix of the
    # new, distinct stream name "SentinelGridTelemetryStream", which is
    # expected and correct.
    for match_start in (m.start() for m in re.finditer("SentinelGridTelemetry", raw)):
        assert raw[match_start : match_start + len("SentinelGridTelemetryStream")] == "SentinelGridTelemetryStream"


def test_outputs_expose_stream_and_rule_identifiers():
    template = _load_template()
    outputs = template["Outputs"]
    assert outputs["StreamName"]["Value"] == {"Ref": "TelemetryStream"}
    assert outputs["StreamArn"]["Value"] == {"Fn::GetAtt": ["TelemetryStream", "Arn"]}
    assert outputs["RuleArn"]["Value"] == {"Fn::GetAtt": ["TelemetryToKinesisRule", "Arn"]}


def test_top_level_description_within_limit():
    template = _load_template()
    assert len(template["Description"]) <= 1024


# --- Deployment script ---


def test_deploy_script_uses_correct_conventions():
    script = DEPLOY_SCRIPT_PATH.read_text()
    assert 'AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"' in script
    assert 'AWS_REGION="${AWS_REGION:-ap-south-1}"' in script
    assert 'STACK_NAME="${STACK_NAME:-sentinelgrid-telemetry-kinesis-stream}"' in script
    assert "CAPABILITY_NAMED_IAM" in script
    assert "telemetry-kinesis-stream.yaml" in script


def test_deploy_script_does_not_reference_other_stack_names():
    script = DEPLOY_SCRIPT_PATH.read_text()
    other_stacks = [
        "sentinelgrid-anomaly-detector",
        "sentinelgrid-anomaly-events-storage",
        "sentinelgrid-anomaly-detection-rule",
        "sentinelgrid-telemetry-storage",
        "sentinelgrid-remediation-workflow",
        "sentinelgrid-remediation-lambdas",
        "sentinelgrid-remediation-trigger",
    ]
    for stack in other_stacks:
        assert stack not in script


# --- M. Existing infrastructure templates remain unchanged (file-level) ---


def test_existing_infrastructure_templates_still_exist_and_parse():
    for filename in EXISTING_TEMPLATES:
        path = REPO_ROOT / "infrastructure" / filename
        assert path.exists(), f"{filename} is missing"
        load_cfn_template(path)  # must not raise


def test_existing_templates_resource_types_unchanged():
    expected_resource_types = {
        "telemetry-storage.yaml": {"AWS::DynamoDB::Table", "AWS::IAM::Role", "AWS::IoT::TopicRule"},
        "anomaly-detection-rule.yaml": {"AWS::Lambda::Permission", "AWS::IoT::TopicRule"},
        "anomaly-events-storage.yaml": {"AWS::DynamoDB::Table"},
        "remediation-lambdas.yaml": {"AWS::IAM::Role", "AWS::Lambda::Function"},
        "remediation-trigger.yaml": {"AWS::IAM::Role", "AWS::Lambda::Function"},
        "remediation-workflow.yaml": {"AWS::IAM::Role", "AWS::StepFunctions::StateMachine"},
    }
    for filename, expected_types in expected_resource_types.items():
        template = load_cfn_template(REPO_ROOT / "infrastructure" / filename)
        actual_types = {r["Type"] for r in template["Resources"].values()}
        assert actual_types == expected_types, f"{filename} resource types changed: {actual_types}"
