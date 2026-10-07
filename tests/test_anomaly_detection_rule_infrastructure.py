"""Structural tests for the IoT -> Lambda anomaly-detection rule
(infrastructure/anomaly-detection-rule.yaml).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no IoT/Lambda access, no
credentials. They also assert the existing telemetry-to-DynamoDB
infrastructure is untouched by this new template.
"""

from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
NEW_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "anomaly-detection-rule.yaml"
DYNAMODB_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "telemetry-storage.yaml"
DETECTOR_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "anomaly-detector.yaml"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-anomaly-detection-rule.sh"


def _load_new_template() -> dict:
    return load_cfn_template(NEW_TEMPLATE_PATH)


def _find_resource_by_type(resources: dict, resource_type: str) -> dict:
    matches = [r for r in resources.values() if r.get("Type") == resource_type]
    assert len(matches) == 1, f"expected exactly one {resource_type}, found {len(matches)}"
    return matches[0]


# --- Template existence / structure ---


def test_template_file_exists():
    assert NEW_TEMPLATE_PATH.exists()


def test_template_parses_and_has_expected_top_level_sections():
    template = _load_new_template()
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Resources" in template


# --- IoT Topic Rule ---


def test_topic_rule_has_expected_logical_id_and_physical_name():
    template = _load_new_template()
    resources = template["Resources"]

    assert "AnomalyDetectionTopicRule" in resources
    rule = resources["AnomalyDetectionTopicRule"]
    assert rule["Type"] == "AWS::IoT::TopicRule"
    assert rule["Properties"]["RuleName"] == "sentinelgrid_telemetry_to_lambda"


def test_topic_rule_listens_to_expected_topic_and_sql_version():
    resources = _load_new_template()["Resources"]
    rule = resources["AnomalyDetectionTopicRule"]

    payload = rule["Properties"]["TopicRulePayload"]
    assert payload["AwsIotSqlVersion"] == "2016-03-23"
    assert payload["Sql"] == "SELECT * FROM 'sentinelgrid/telemetry'"
    # (Step 26) Disabled, not deleted: this direct IoT -> Analyzer path is
    # superseded by SQS -> SentinelGridSqsTelemetryConsumer -> Analyzer
    # (see infrastructure/sqs-consumer.yaml), to avoid duplicate analyzer
    # processing of the same telemetry message. The rule resource and its
    # Lambda action are preserved for historical traceability.
    assert payload["RuleDisabled"] is True


def test_topic_rule_invokes_the_anomaly_detector_lambda():
    resources = _load_new_template()["Resources"]
    rule = resources["AnomalyDetectionTopicRule"]

    actions = rule["Properties"]["TopicRulePayload"]["Actions"]
    assert len(actions) == 1
    action = actions[0]

    assert "Lambda" in action
    function_arn = action["Lambda"]["FunctionArn"]
    # Built via Fn::Sub referencing the AnomalyDetectorFunctionName
    # parameter, not a hardcoded function name.
    assert "Fn::Sub" in function_arn
    sub_expression = function_arn["Fn::Sub"]
    assert "SentinelGridAnomalyDetector" not in sub_expression
    assert "${AnomalyDetectorFunctionName}" in sub_expression
    assert ":lambda:" in sub_expression


def test_topic_rule_depends_on_the_lambda_permission():
    resources = _load_new_template()["Resources"]
    rule = resources["AnomalyDetectionTopicRule"]

    depends_on = rule.get("DependsOn")
    depends_on = depends_on if isinstance(depends_on, list) else [depends_on]
    assert "AnomalyDetectorInvokePermission" in depends_on


# --- Lambda permission ---


def test_lambda_permission_exists_with_expected_shape():
    resources = _load_new_template()["Resources"]
    permission = _find_resource_by_type(resources, "AWS::Lambda::Permission")
    props = permission["Properties"]

    assert props["Action"] == "lambda:InvokeFunction"
    assert props["Principal"] == "iot.amazonaws.com"


def test_lambda_permission_targets_the_anomaly_detector_function():
    resources = _load_new_template()["Resources"]
    permission = _find_resource_by_type(resources, "AWS::Lambda::Permission")

    assert permission["Properties"]["FunctionName"] == {"Ref": "AnomalyDetectorFunctionName"}


def test_lambda_permission_is_scoped_to_the_specific_rule_not_public():
    resources = _load_new_template()["Resources"]
    permission = _find_resource_by_type(resources, "AWS::Lambda::Permission")
    props = permission["Properties"]

    assert props["Principal"] != "*"
    assert "SourceArn" in props
    source_arn = props["SourceArn"]
    assert "Fn::Sub" in source_arn
    assert "sentinelgrid_telemetry_to_lambda" in source_arn["Fn::Sub"]
    assert ":iot:" in source_arn["Fn::Sub"]
    assert "rule/" in source_arn["Fn::Sub"]


def test_default_function_name_parameter_matches_deployed_function():
    params = _load_new_template()["Parameters"]
    assert params["AnomalyDetectorFunctionName"]["Default"] == "SentinelGridAnomalyDetector"


# --- No unexpected IAM / roles introduced ---


def test_no_iam_role_is_created_by_this_template():
    # The IoT "lambda" action authorizes via a Lambda resource policy, not
    # an assumed role — no AWS::IAM::Role should exist here.
    resources = _load_new_template()["Resources"]
    roles = [r for r in resources.values() if r.get("Type") == "AWS::IAM::Role"]
    assert roles == []


def test_no_administrator_access_or_wildcard_permissions_anywhere_in_template():
    raw_text = NEW_TEMPLATE_PATH.read_text()
    assert "AdministratorAccess" not in raw_text

    # No IAM Action/Resource wildcards (this template shouldn't define any
    # IAM policy statements at all, but guard against one creeping in).
    resources = _load_new_template()["Resources"]
    for resource in resources.values():
        if resource.get("Type") == "AWS::IAM::Role":
            for policy in resource["Properties"].get("Policies", []):
                for statement in policy["PolicyDocument"]["Statement"]:
                    actions = statement["Action"]
                    actions = actions if isinstance(actions, list) else [actions]
                    assert "*" not in actions


def test_no_hardcoded_credentials_or_account_ids_in_template():
    raw_text = NEW_TEMPLATE_PATH.read_text().lower()
    for forbidden in ("akia", "secret", "access_key", "accesskey", "password", "arn:aws:iam::123456789012"):
        assert forbidden not in raw_text


# --- No result persistence / remediation resources introduced ---


def test_no_dynamodb_stepfunctions_sns_or_cloudwatch_alarm_resources():
    resources = _load_new_template()["Resources"]
    forbidden_types = (
        "AWS::DynamoDB::Table",
        "AWS::StepFunctions::StateMachine",
        "AWS::SNS::Topic",
        "AWS::CloudWatch::Alarm",
    )
    for resource in resources.values():
        assert resource.get("Type") not in forbidden_types


# --- Existing infrastructure is untouched ---


def test_existing_dynamodb_telemetry_storage_template_is_unmodified():
    # This new template must not define/duplicate/alter the existing
    # DynamoDB table or its IoT rule/role.
    new_resources = _load_new_template()["Resources"]
    for logical_id in ("TelemetryTable", "IoTToDynamoDBRole", "TelemetryToDynamoDBRule"):
        assert logical_id not in new_resources

    dynamodb_template = load_cfn_template(DYNAMODB_TEMPLATE_PATH)
    rule = dynamodb_template["Resources"]["TelemetryToDynamoDBRule"]
    assert rule["Properties"]["RuleName"] == "sentinelgrid_telemetry_to_dynamodb"
    # The two rules must be distinct physical names.
    new_rule = _load_new_template()["Resources"]["AnomalyDetectionTopicRule"]
    assert new_rule["Properties"]["RuleName"] != rule["Properties"]["RuleName"]


def test_existing_anomaly_detector_function_template_is_unmodified():
    detector_template = load_cfn_template(DETECTOR_TEMPLATE_PATH)
    resources = detector_template["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    assert function["Properties"]["FunctionName"] == "SentinelGridAnomalyDetector"
    # This new template must not redefine the function or its role.
    new_resources = _load_new_template()["Resources"]
    assert "AnomalyDetectorFunction" not in new_resources
    assert "AnomalyDetectorExecutionRole" not in new_resources


# --- Deployment helper script ---


def test_deploy_script_exists_and_is_executable():
    assert DEPLOY_SCRIPT.exists()
    assert DEPLOY_SCRIPT.stat().st_mode & 0o111


def test_deploy_script_uses_sentinelgrid_profile_and_ap_south_1_by_default():
    text = DEPLOY_SCRIPT.read_text()
    assert "AWS_PROFILE:-sentinelgrid" in text
    assert "AWS_REGION:-ap-south-1" in text


def test_deploy_script_contains_no_hardcoded_credentials():
    raw_text = DEPLOY_SCRIPT.read_text().lower()
    for forbidden in ("akia", "aws_secret_access_key=", "aws_access_key_id="):
        assert forbidden not in raw_text


def test_deploy_script_does_not_touch_dynamodb_stack():
    text = DEPLOY_SCRIPT.read_text()
    assert "telemetry-storage" not in text
    assert "sentinelgrid-telemetry-storage" not in text
