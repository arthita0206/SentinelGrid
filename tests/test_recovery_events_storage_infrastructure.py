"""Structural tests for the recovery-events-storage infrastructure
(infrastructure/recovery-events-storage.yaml and its deploy script).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no DynamoDB access, no credentials,
and no deployment. Mirrors
tests/test_anomaly_events_storage_infrastructure.py's own conventions
exactly, applied to the new Step 33 table.
"""

from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "recovery-events-storage.yaml"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-recovery-events-storage.sh"
TELEMETRY_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "telemetry-storage.yaml"
ANOMALY_EVENTS_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "anomaly-events-storage.yaml"
REMEDIATION_LAMBDAS_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "remediation-lambdas.yaml"


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
    assert props["TableName"] == "SentinelGridRecoveryEvents"
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert "ProvisionedThroughput" not in props


def test_table_encryption_is_enabled():
    resources = _load_template()["Resources"]
    table = _find_resource_by_type(resources, "AWS::DynamoDB::Table")
    props = table["Properties"]

    assert props["SSESpecification"]["SSEEnabled"] is True


def test_table_arn_is_exported_for_cross_stack_use():
    # The remediation-lambdas stack imports this exact export name to
    # grant Planner/Executor least-privilege write access.
    template = _load_template()
    export = template["Outputs"]["RecoveryEventsTableArn"]["Export"]

    assert export["Name"] == "SentinelGridRecoveryEventsTableArn"


def test_table_name_is_exported_for_cross_stack_use():
    # The remediation-lambdas stack imports this to populate Planner/
    # Executor's RECOVERY_EVENTS_TABLE_NAME environment variable, so the
    # table's fixed name is referenced via CloudFormation rather than
    # hardcoded a second time in that template.
    template = _load_template()
    export = template["Outputs"]["RecoveryEventsTableName"]["Export"]

    assert export["Name"] == "SentinelGridRecoveryEventsTableName"


def test_partition_and_sort_key_are_correctly_typed():
    resources = _load_template()["Resources"]
    table = _find_resource_by_type(resources, "AWS::DynamoDB::Table")
    props = table["Properties"]

    attr_types = {a["AttributeName"]: a["AttributeType"] for a in props["AttributeDefinitions"]}
    assert attr_types == {"node_id": "S", "timestamp": "S"}

    key_types = {k["AttributeName"]: k["KeyType"] for k in props["KeySchema"]}
    assert key_types == {"node_id": "HASH", "timestamp": "RANGE"}


def test_table_data_survives_stack_deletion():
    # SentinelGridRecoveryEvents is an audit/history table — its data
    # must not be destroyed by a stack deletion. CloudFormation's default
    # DeletionPolicy (Delete) would do exactly that, so this must be
    # explicitly overridden.
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
    assert "sentinelgrid-recovery-events-storage" in text


def test_deploy_script_uses_sentinelgrid_profile_and_ap_south_1_by_default():
    text = DEPLOY_SCRIPT.read_text()
    assert "AWS_PROFILE:-sentinelgrid" in text
    assert "AWS_REGION:-ap-south-1" in text


def test_deploy_script_references_the_correct_template():
    text = DEPLOY_SCRIPT.read_text()
    assert "recovery-events-storage.yaml" in text


def test_deploy_script_contains_no_hardcoded_credentials():
    raw_text = DEPLOY_SCRIPT.read_text().lower()
    for forbidden in ("akia", "aws_secret_access_key=", "aws_access_key_id="):
        assert forbidden not in raw_text


def test_deploy_script_does_not_touch_other_stacks_or_tables():
    text = DEPLOY_SCRIPT.read_text()
    assert "SentinelGridTelemetry" not in text
    assert "SentinelGridAnomalyEvents" not in text
    assert "sentinelgrid-telemetry-storage" not in text
    assert "sentinelgrid-anomaly-detector" not in text
    assert "sentinelgrid-remediation-lambdas" not in text
    assert "update-function-code" not in text


# --- Isolation from existing infrastructure ---


def test_existing_templates_are_not_referenced_or_duplicated():
    new_resources = _load_template()["Resources"]
    assert "TelemetryTable" not in new_resources
    assert "AnomalyEventsTable" not in new_resources
    assert "PlannerFunction" not in new_resources
    assert "ExecutorFunction" not in new_resources

    # The existing templates themselves are untouched by this change (spot
    # check their defining resources are still exactly as before).
    telemetry_resources = load_cfn_template(TELEMETRY_TEMPLATE_PATH)["Resources"]
    assert telemetry_resources["TelemetryTable"]["Properties"]["TableName"] == "SentinelGridTelemetry"

    anomaly_events_resources = load_cfn_template(ANOMALY_EVENTS_TEMPLATE_PATH)["Resources"]
    assert anomaly_events_resources["AnomalyEventsTable"]["Properties"]["TableName"] == "SentinelGridAnomalyEvents"
