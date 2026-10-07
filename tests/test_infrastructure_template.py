"""Structural tests for infrastructure/telemetry-storage.yaml (Step 5).

These tests parse the CloudFormation template and assert its shape. They
do NOT call AWS in any way (no `aws cloudformation validate-template`, no
boto3 CloudFormation client) — the template is never deployed or
validated against a real account by this test suite.
"""

from pathlib import Path

from tests._cfn_yaml import load_cfn_template

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "infrastructure" / "telemetry-storage.yaml"
)


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def test_template_file_exists():
    assert TEMPLATE_PATH.exists()


def test_template_parses_and_has_expected_top_level_sections():
    template = _load_template()
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Resources" in template


def test_dynamodb_table_matches_required_schema():
    resources = _load_template()["Resources"]
    table = resources["TelemetryTable"]

    assert table["Type"] == "AWS::DynamoDB::Table"
    props = table["Properties"]
    assert props["TableName"] == "SentinelGridTelemetry"
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert "ProvisionedThroughput" not in props

    attr_types = {a["AttributeName"]: a["AttributeType"] for a in props["AttributeDefinitions"]}
    assert attr_types == {"node_id": "S", "timestamp": "S"}

    key_types = {k["AttributeName"]: k["KeyType"] for k in props["KeySchema"]}
    assert key_types == {"node_id": "HASH", "timestamp": "RANGE"}


def test_iam_role_trusts_only_iot_service():
    resources = _load_template()["Resources"]
    role = resources["IoTToDynamoDBRole"]

    assert role["Type"] == "AWS::IAM::Role"
    statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Effect"] == "Allow"
    assert statement["Principal"]["Service"] == "iot.amazonaws.com"
    assert statement["Action"] == "sts:AssumeRole"


def test_iam_role_grants_only_dynamodb_putitem_on_the_telemetry_table():
    resources = _load_template()["Resources"]
    role = resources["IoTToDynamoDBRole"]

    policies = role["Properties"]["Policies"]
    assert len(policies) == 1
    statements = policies[0]["PolicyDocument"]["Statement"]
    assert len(statements) == 1
    statement = statements[0]

    assert statement["Effect"] == "Allow"
    actions = statement["Action"]
    actions = actions if isinstance(actions, list) else [actions]
    assert actions == ["dynamodb:PutItem"]

    # Resource must reference the table's ARN, never a bare wildcard.
    resource = statement["Resource"]
    assert resource != "*"
    assert resource == {"Fn::GetAtt": ["TelemetryTable", "Arn"]}


def test_topic_rule_listens_to_expected_topic_and_sql_version():
    resources = _load_template()["Resources"]
    rule = resources["TelemetryToDynamoDBRule"]

    assert rule["Type"] == "AWS::IoT::TopicRule"
    assert rule["Properties"]["RuleName"] == "sentinelgrid_telemetry_to_dynamodb"

    payload = rule["Properties"]["TopicRulePayload"]
    assert payload["AwsIotSqlVersion"] == "2016-03-23"
    assert "sentinelgrid/telemetry" in payload["Sql"]
    assert payload["RuleDisabled"] is False


def test_topic_rule_uses_dynamodbv2_action_targeting_the_telemetry_table():
    resources = _load_template()["Resources"]
    rule = resources["TelemetryToDynamoDBRule"]

    actions = rule["Properties"]["TopicRulePayload"]["Actions"]
    assert len(actions) == 1
    action = actions[0]

    assert "DynamoDBv2" in action
    dynamo_action = action["DynamoDBv2"]
    assert dynamo_action["PutItem"]["TableName"] == {"Ref": "TelemetryTable"}
    assert dynamo_action["RoleArn"] == {"Fn::GetAtt": ["IoTToDynamoDBRole", "Arn"]}


def test_no_hardcoded_credentials_or_account_ids_in_template():
    raw_text = TEMPLATE_PATH.read_text().lower()
    for forbidden in ("akia", "secret", "access_key", "accesskey", "password", "arn:aws:iam::123456789012"):
        assert forbidden not in raw_text
