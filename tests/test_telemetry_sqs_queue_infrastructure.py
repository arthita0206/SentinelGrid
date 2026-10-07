"""Structural tests for the SQS telemetry ingestion infrastructure
(infrastructure/telemetry-sqs-queue.yaml) and its deployment script.

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3/sqs client, no network access, no
credentials. They validate local YAML structure only.
"""

import re
from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "telemetry-sqs-queue.yaml"
DEPLOY_SCRIPT_PATH = REPO_ROOT / "infrastructure" / "deploy-telemetry-sqs-queue.sh"

EXISTING_TEMPLATES = [
    "telemetry-storage.yaml",
    "anomaly-detector.yaml",
    "anomaly-detection-rule.yaml",
    "anomaly-events-storage.yaml",
    "remediation-workflow.yaml",
    "remediation-lambdas.yaml",
    "remediation-trigger.yaml",
    "telemetry-kinesis-stream.yaml",
]


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _resources_of_type(template: dict, resource_type: str) -> list:
    return [r for r in template["Resources"].values() if r["Type"] == resource_type]


def _queue_by_logical_id(template: dict, logical_id: str) -> dict:
    return template["Resources"][logical_id]


# --- A, B, C. Main SQS queue exists, stable name, Standard type ---


def test_template_parses_and_has_exactly_two_queues():
    assert TEMPLATE_PATH.exists()
    template = _load_template()
    queues = _resources_of_type(template, "AWS::SQS::Queue")
    assert len(queues) == 2


def test_main_queue_has_stable_name_and_is_standard_not_fifo():
    template = _load_template()
    queue = _queue_by_logical_id(template, "TelemetryQueue")
    assert queue["Properties"]["QueueName"] == "SentinelGridTelemetryQueue"
    assert not queue["Properties"]["QueueName"].endswith(".fifo")
    assert "FifoQueue" not in queue["Properties"]


def test_main_queue_has_retain_deletion_policy():
    template = _load_template()
    queue = template["Resources"]["TelemetryQueue"]
    assert queue.get("DeletionPolicy") == "Retain"
    assert queue.get("UpdateReplacePolicy") == "Retain"


# --- D. Message retention configured ---


def test_message_retention_is_a_parameter_with_sensible_default():
    template = _load_template()
    params = template["Parameters"]
    assert "MessageRetentionPeriodSeconds" in params
    assert params["MessageRetentionPeriodSeconds"]["Default"] == 345600  # 4 days

    queue = _queue_by_logical_id(template, "TelemetryQueue")
    assert queue["Properties"]["MessageRetentionPeriod"] == {"Ref": "MessageRetentionPeriodSeconds"}


def test_retention_is_not_hardcoded_in_application_code():
    for py_file in (REPO_ROOT / "simulator").glob("*.py"):
        content = py_file.read_text()
        assert "MessageRetentionPeriod" not in content


# --- E. Visibility timeout configured ---


def test_visibility_timeout_is_a_parameter_with_sensible_default():
    template = _load_template()
    params = template["Parameters"]
    assert "VisibilityTimeoutSeconds" in params
    # (Step 26) Raised from SQS's own default of 30 to 90: AWS Lambda's
    # SQS event source mapping requires VisibilityTimeout >= the consumer
    # Lambda's own Timeout (60s) — see infrastructure/sqs-consumer.yaml
    # and this project's own live CreateEventSourceMapping rejection at
    # 30s before this change.
    assert params["VisibilityTimeoutSeconds"]["Default"] == 90

    queue = _queue_by_logical_id(template, "TelemetryQueue")
    assert queue["Properties"]["VisibilityTimeout"] == {"Ref": "VisibilityTimeoutSeconds"}


# --- F, G. DLQ exists and main queue has a redrive policy pointing to it ---


def test_dead_letter_queue_exists_with_stable_name():
    template = _load_template()
    dlq = _queue_by_logical_id(template, "TelemetryDLQ")
    assert dlq["Properties"]["QueueName"] == "SentinelGridTelemetryDLQ"
    assert dlq.get("DeletionPolicy") == "Retain"


def test_exactly_one_dlq_exists():
    template = _load_template()
    queue_names = {r["Properties"]["QueueName"] for r in _resources_of_type(template, "AWS::SQS::Queue")}
    dlq_like = [n for n in queue_names if "DLQ" in n or "DeadLetter" in n]
    assert len(dlq_like) == 1


def test_main_queue_redrive_policy_points_to_the_dlq_with_sensible_max_receive_count():
    template = _load_template()
    queue = _queue_by_logical_id(template, "TelemetryQueue")
    redrive = queue["Properties"]["RedrivePolicy"]
    assert redrive["deadLetterTargetArn"] == {"Fn::GetAtt": ["TelemetryDLQ", "Arn"]}
    assert redrive["maxReceiveCount"] == {"Ref": "MaxReceiveCount"}

    params = template["Parameters"]
    assert "MaxReceiveCount" in params
    assert params["MaxReceiveCount"]["Default"] == 5


# --- H, I. IoT rule consumes sentinelgrid/telemetry with SELECT * ---


def test_rule_consumes_the_existing_telemetry_topic_with_select_star():
    template = _load_template()
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")[0]
    sql = rule["Properties"]["TopicRulePayload"]["Sql"]
    assert sql == "SELECT * FROM 'sentinelgrid/telemetry'"
    assert rule["Properties"]["TopicRulePayload"]["RuleDisabled"] is False


def test_rule_name_is_distinct_from_existing_rules():
    template = _load_template()
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")[0]
    rule_name = rule["Properties"]["RuleName"]
    assert rule_name == "sentinelgrid_telemetry_to_sqs"
    assert rule_name not in ("sentinelgrid_telemetry_to_dynamodb", "sentinelgrid_telemetry_to_lambda")


# --- J, K. IoT rule sends to SQS; message body preserves full payload ---


def test_rule_action_is_sqs_targeting_the_main_queue():
    template = _load_template()
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")[0]
    actions = rule["Properties"]["TopicRulePayload"]["Actions"]
    assert len(actions) == 1
    assert "Sqs" in actions[0]
    assert actions[0]["Sqs"]["QueueUrl"] == {"Ref": "TelemetryQueue"}


def test_sqs_action_sends_raw_payload_not_base64_wrapped():
    # UseBase64: false means the message body is the exact telemetry JSON
    # text SELECT * captured — no additional encoding/wrapping layer, so
    # node_id/subsystem/timestamp/status/data all survive untouched.
    template = _load_template()
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")[0]
    action = rule["Properties"]["TopicRulePayload"]["Actions"][0]["Sqs"]
    assert action["UseBase64"] is False


# --- L, M, N, O. IAM least privilege ---


def _role_properties(template: dict) -> dict:
    return _resources_of_type(template, "AWS::IAM::Role")[0]["Properties"]


def test_exactly_one_iam_role_trusting_only_iot():
    template = _load_template()
    roles = _resources_of_type(template, "AWS::IAM::Role")
    assert len(roles) == 1
    statements = roles[0]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    assert statements[0]["Principal"] == {"Service": "iot.amazonaws.com"}


def test_role_grants_exactly_sqs_sendmessage():
    template = _load_template()
    props = _role_properties(template)
    policies = props.get("Policies", [])
    assert len(policies) == 1
    statement = policies[0]["PolicyDocument"]["Statement"][0]
    actions = statement["Action"]
    actions = actions if isinstance(actions, list) else [actions]
    assert actions == ["sqs:SendMessage"]


def test_iam_resource_is_the_exact_main_queue_arn_not_wildcard():
    template = _load_template()
    props = _role_properties(template)
    statement = props["Policies"][0]["PolicyDocument"]["Statement"][0]
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert resources == [{"Fn::GetAtt": ["TelemetryQueue", "Arn"]}]
    assert "*" not in resources


def test_no_wildcard_sqs_permission_anywhere():
    raw = TEMPLATE_PATH.read_text()
    assert "sqs:*" not in raw
    assert '"*"' not in raw
    assert not re.search(r"Resource:\s*\*\s*$", raw, re.MULTILINE)


# --- P. No Kinesis resources introduced ---


def test_no_kinesis_resources_introduced():
    template = _load_template()
    assert _resources_of_type(template, "AWS::Kinesis::Stream") == []
    raw = TEMPLATE_PATH.read_text()
    assert "kinesis:" not in raw


# --- Q. No Lambda/Step Functions/DynamoDB permissions ---


def test_no_unrelated_permissions_introduced():
    raw = TEMPLATE_PATH.read_text()
    for forbidden_prefix in (
        "lambda:",
        "states:",
        "dynamodb:",
        "iam:",
        "s3:",
        "sns:",
        "ec2:",
        "cloudformation:",
    ):
        assert forbidden_prefix not in raw, f"found forbidden permission prefix {forbidden_prefix!r}"
    assert "AdministratorAccess" not in raw
    assert "PowerUserAccess" not in raw


# --- Encryption ---


def test_both_queues_use_sqs_managed_encryption():
    template = _load_template()
    for logical_id in ("TelemetryQueue", "TelemetryDLQ"):
        queue = _queue_by_logical_id(template, logical_id)
        assert queue["Properties"]["SqsManagedSseEnabled"] is True


def test_no_customer_managed_kms_key_resource_created():
    template = _load_template()
    assert _resources_of_type(template, "AWS::KMS::Key") == []


# --- Only expected resource types; no unrelated infra referenced ---


def test_only_expected_resource_types_are_created():
    template = _load_template()
    actual_types = {r["Type"] for r in template["Resources"].values()}
    assert actual_types == {"AWS::SQS::Queue", "AWS::IAM::Role", "AWS::IoT::TopicRule"}


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
        "SentinelGridTelemetryStream",  # the Kinesis stream from Step 24
    ):
        assert name not in raw, f"unexpectedly references {name!r}"


def test_outputs_expose_queue_dlq_and_rule_identifiers():
    template = _load_template()
    outputs = template["Outputs"]
    assert outputs["QueueUrl"]["Value"] == {"Ref": "TelemetryQueue"}
    assert outputs["QueueArn"]["Value"] == {"Fn::GetAtt": ["TelemetryQueue", "Arn"]}
    assert outputs["DeadLetterQueueUrl"]["Value"] == {"Ref": "TelemetryDLQ"}
    assert outputs["DeadLetterQueueArn"]["Value"] == {"Fn::GetAtt": ["TelemetryDLQ", "Arn"]}
    assert outputs["RuleArn"]["Value"] == {"Fn::GetAtt": ["TelemetryToSqsRule", "Arn"]}


def test_top_level_description_within_limit():
    template = _load_template()
    assert len(template["Description"]) <= 1024


# --- Deployment script ---


def test_deploy_script_uses_correct_conventions():
    script = DEPLOY_SCRIPT_PATH.read_text()
    assert 'AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"' in script
    assert 'AWS_REGION="${AWS_REGION:-ap-south-1}"' in script
    assert 'STACK_NAME="${STACK_NAME:-sentinelgrid-telemetry-sqs-queue}"' in script
    assert "CAPABILITY_NAMED_IAM" in script
    assert "telemetry-sqs-queue.yaml" in script


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
        "sentinelgrid-telemetry-kinesis-stream",
    ]
    for stack in other_stacks:
        assert stack not in script


def test_deploy_script_does_not_retry_indefinitely():
    # No loop construct of any kind — a failed `aws cloudformation deploy`
    # exits immediately via `set -e` / the explicit `if ! ... ; then exit 1`
    # guard. (The word "retry" itself legitimately appears in this
    # script's own comment documenting that it deliberately does NOT
    # retry — that's the correct, intended behavior, not a loop.)
    script = DEPLOY_SCRIPT_PATH.read_text()
    assert "while" not in script.lower()
    assert "until" not in script.lower()


# --- R. Existing infrastructure templates remain unchanged (file-level) ---


def test_existing_infrastructure_templates_still_exist_and_parse():
    for filename in EXISTING_TEMPLATES:
        path = REPO_ROOT / "infrastructure" / filename
        assert path.exists(), f"{filename} is missing"
        load_cfn_template(path)  # must not raise


def test_kinesis_step24_files_are_preserved_not_deleted():
    assert (REPO_ROOT / "infrastructure" / "telemetry-kinesis-stream.yaml").exists()
    assert (REPO_ROOT / "infrastructure" / "deploy-telemetry-kinesis-stream.sh").exists()
    assert (REPO_ROOT / "tests" / "test_telemetry_kinesis_stream_infrastructure.py").exists()


def test_existing_templates_resource_types_unchanged():
    expected_resource_types = {
        "telemetry-storage.yaml": {"AWS::DynamoDB::Table", "AWS::IAM::Role", "AWS::IoT::TopicRule"},
        "anomaly-detection-rule.yaml": {"AWS::Lambda::Permission", "AWS::IoT::TopicRule"},
        "anomaly-events-storage.yaml": {"AWS::DynamoDB::Table"},
        "remediation-lambdas.yaml": {"AWS::IAM::Role", "AWS::Lambda::Function"},
        "remediation-trigger.yaml": {"AWS::IAM::Role", "AWS::Lambda::Function"},
        "remediation-workflow.yaml": {"AWS::IAM::Role", "AWS::StepFunctions::StateMachine"},
        "telemetry-kinesis-stream.yaml": {"AWS::Kinesis::Stream", "AWS::IAM::Role", "AWS::IoT::TopicRule"},
    }
    for filename, expected_types in expected_resource_types.items():
        template = load_cfn_template(REPO_ROOT / "infrastructure" / filename)
        actual_types = {r["Type"] for r in template["Resources"].values()}
        assert actual_types == expected_types, f"{filename} resource types changed: {actual_types}"
