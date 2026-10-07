"""Structural tests for the node-state-storage infrastructure
(infrastructure/node-state-storage.yaml and its deploy script).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no DynamoDB access, no credentials,
and no deployment. Mirrors
tests/test_recovery_events_storage_infrastructure.py's own conventions,
applied to the new Step 34 table.
"""

from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "node-state-storage.yaml"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-node-state-storage.sh"
TELEMETRY_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "telemetry-storage.yaml"
ANOMALY_EVENTS_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "anomaly-events-storage.yaml"
RECOVERY_EVENTS_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "recovery-events-storage.yaml"


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _find_resource_by_type(resources: dict, resource_type: str) -> dict:
    matches = [r for r in resources.values() if r.get("Type") == resource_type]
    assert len(matches) == 1, f"expected exactly one {resource_type}, found {len(matches)}"
    return matches[0]


# --- Template structure ---


def test_template_file_exists():
    assert TEMPLATE_PATH.exists()


def test_template_parses_and_has_expected_top_level_sections():
    template = _load_template()
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Resources" in template


# --- DynamoDB table schema ---


def test_dynamodb_table_matches_required_schema():
    resources = _load_template()["Resources"]
    table = _find_resource_by_type(resources, "AWS::DynamoDB::Table")

    props = table["Properties"]
    assert props["TableName"] == "SentinelGridNodeState"
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert "ProvisionedThroughput" not in props


def test_table_encryption_is_enabled():
    resources = _load_template()["Resources"]
    table = _find_resource_by_type(resources, "AWS::DynamoDB::Table")
    props = table["Properties"]

    assert props["SSESpecification"]["SSEEnabled"] is True


def test_table_arn_and_name_are_exported_for_cross_stack_use():
    template = _load_template()
    arn_export = template["Outputs"]["NodeStateTableArn"]["Export"]
    name_export = template["Outputs"]["NodeStateTableName"]["Export"]

    assert arn_export["Name"] == "SentinelGridNodeStateTableArn"
    assert name_export["Name"] == "SentinelGridNodeStateTableName"


def test_primary_key_is_node_id_only_no_sort_key():
    resources = _load_template()["Resources"]
    table = _find_resource_by_type(resources, "AWS::DynamoDB::Table")
    props = table["Properties"]

    attr_types = {a["AttributeName"]: a["AttributeType"] for a in props["AttributeDefinitions"]}
    assert attr_types == {"node_id": "S"}

    key_schema = props["KeySchema"]
    assert len(key_schema) == 1
    assert key_schema[0] == {"AttributeName": "node_id", "KeyType": "HASH"}
    assert not any(k["KeyType"] == "RANGE" for k in key_schema)


def test_table_data_survives_stack_deletion():
    resources = _load_template()["Resources"]
    table = _find_resource_by_type(resources, "AWS::DynamoDB::Table")
    assert table["DeletionPolicy"] == "Retain"


def test_table_data_survives_resource_replacement():
    resources = _load_template()["Resources"]
    table = _find_resource_by_type(resources, "AWS::DynamoDB::Table")
    assert table["UpdateReplacePolicy"] == "Retain"


def test_no_unnecessary_global_secondary_indexes():
    resources = _load_template()["Resources"]
    table = _find_resource_by_type(resources, "AWS::DynamoDB::Table")
    props = table["Properties"]

    assert "GlobalSecondaryIndexes" not in props
    assert "LocalSecondaryIndexes" not in props


def test_table_is_the_only_resource_no_iam_or_iot_created():
    resources = _load_template()["Resources"]
    assert len(resources) == 1
    for resource in resources.values():
        assert resource["Type"] not in ("AWS::IAM::Role", "AWS::IoT::TopicRule", "AWS::Lambda::Function")


def test_no_hardcoded_credentials_or_account_ids_in_template():
    raw_text = TEMPLATE_PATH.read_text().lower()
    for forbidden in ("akia", "secret", "access_key", "accesskey", "password", "arn:aws:iam::123456789012"):
        assert forbidden not in raw_text


def test_top_level_description_within_limit():
    template = _load_template()
    assert len(template["Description"]) <= 1024


# --- Deployment script ---


def test_deploy_script_exists_and_is_executable():
    assert DEPLOY_SCRIPT.exists()
    assert DEPLOY_SCRIPT.stat().st_mode & 0o111


def test_deploy_script_uses_expected_stack_name():
    text = DEPLOY_SCRIPT.read_text()
    assert "sentinelgrid-node-state-storage" in text


def test_deploy_script_uses_sentinelgrid_profile_and_ap_south_1_by_default():
    text = DEPLOY_SCRIPT.read_text()
    assert "AWS_PROFILE:-sentinelgrid" in text
    assert "AWS_REGION:-ap-south-1" in text


def test_deploy_script_references_the_correct_template():
    text = DEPLOY_SCRIPT.read_text()
    assert "node-state-storage.yaml" in text


def test_deploy_script_contains_no_hardcoded_credentials():
    raw_text = DEPLOY_SCRIPT.read_text().lower()
    for forbidden in ("akia", "aws_secret_access_key=", "aws_access_key_id="):
        assert forbidden not in raw_text


def test_deploy_script_does_not_touch_other_stacks_or_tables():
    text = DEPLOY_SCRIPT.read_text()
    assert "SentinelGridTelemetry" not in text
    assert "SentinelGridAnomalyEvents" not in text
    assert "SentinelGridRecoveryEvents" not in text
    assert "sentinelgrid-telemetry-storage" not in text
    assert "sentinelgrid-anomaly-detector" not in text
    assert "sentinelgrid-remediation-lambdas" not in text
    assert "update-function-code" not in text


# --- Isolation from existing infrastructure ---


def test_existing_templates_are_not_referenced_or_duplicated():
    new_resources = _load_template()["Resources"]
    assert "TelemetryTable" not in new_resources
    assert "AnomalyEventsTable" not in new_resources
    assert "RecoveryEventsTable" not in new_resources

    telemetry_resources = load_cfn_template(TELEMETRY_TEMPLATE_PATH)["Resources"]
    assert telemetry_resources["TelemetryTable"]["Properties"]["TableName"] == "SentinelGridTelemetry"

    anomaly_events_resources = load_cfn_template(ANOMALY_EVENTS_TEMPLATE_PATH)["Resources"]
    assert anomaly_events_resources["AnomalyEventsTable"]["Properties"]["TableName"] == "SentinelGridAnomalyEvents"

    recovery_events_resources = load_cfn_template(RECOVERY_EVENTS_TEMPLATE_PATH)["Resources"]
    assert recovery_events_resources["RecoveryEventsTable"]["Properties"]["TableName"] == "SentinelGridRecoveryEvents"
