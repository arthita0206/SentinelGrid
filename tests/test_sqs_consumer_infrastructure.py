"""Structural tests for the SQS consumer infrastructure
(infrastructure/sqs-consumer.yaml) and its packaging script
(infrastructure/package-sqs-consumer.sh).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no S3/Lambda access, no
credentials. The packaging test does invoke the local shell script, but
that script only copies local files and runs `zip` — it makes no
network calls.
"""

import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "sqs-consumer.yaml"
PACKAGE_SCRIPT = REPO_ROOT / "infrastructure" / "package-sqs-consumer.sh"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-sqs-consumer.sh"
RULE_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "anomaly-detection-rule.yaml"

REQUIRED_ZIP_ENTRIES = {
    "lambda_functions/__init__.py",
    "lambda_functions/sqs_consumer/__init__.py",
    "lambda_functions/sqs_consumer/handler.py",
    "lambda_functions/sqs_consumer/anomaly_detector_client.py",
}
FORBIDDEN_ZIP_SUBSTRINGS = ("venv/", "tests/", ".git/", "logs/")


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _resources_of_type(template: dict, resource_type: str) -> list:
    return [r for r in template["Resources"].values() if r["Type"] == resource_type]


def _role_properties(template: dict) -> dict:
    return _resources_of_type(template, "AWS::IAM::Role")[0]["Properties"]


def _function_properties(template: dict) -> dict:
    return _resources_of_type(template, "AWS::Lambda::Function")[0]["Properties"]


def _mapping_properties(template: dict) -> dict:
    return _resources_of_type(template, "AWS::Lambda::EventSourceMapping")[0]["Properties"]


def _find_policy(props: dict, name: str) -> dict:
    matches = [p for p in props["Policies"] if p["PolicyName"] == name]
    assert len(matches) == 1
    return matches[0]


# --- A. Consumer Lambda exists ---


def test_template_parses_exactly_one_function_role_and_mapping():
    template = _load_template()
    assert len(_resources_of_type(template, "AWS::Lambda::Function")) == 1
    assert len(_resources_of_type(template, "AWS::IAM::Role")) == 1
    assert len(_resources_of_type(template, "AWS::Lambda::EventSourceMapping")) == 1


# --- B. Correct runtime / architecture / handler ---


def test_function_runtime_config():
    props = _function_properties(_load_template())
    assert props["Runtime"] == "python3.12"
    assert props["Architectures"] == ["arm64"]
    assert props["MemorySize"] == 128
    assert props["Handler"] == "lambda_functions.sqs_consumer.handler.lambda_handler"


def test_function_timeout_is_consistent_with_batch_size_and_analyzer_ceiling():
    template = _load_template()
    props = _function_properties(template)
    batch_size = template["Parameters"]["BatchSize"]["Default"]
    # Worst case: BatchSize sequential synchronous Analyzer invokes, each
    # up to the Analyzer's own 10s Lambda timeout ceiling.
    worst_case_seconds = int(batch_size) * 10
    assert props["Timeout"] >= worst_case_seconds
    assert props["Timeout"] <= 900  # sane upper bound; not AWS's absolute max


def test_function_physical_name():
    props = _function_properties(_load_template())
    assert props["FunctionName"] == "SentinelGridSqsTelemetryConsumer"


# --- C. Execution role exists, trusts only Lambda ---


def test_role_trusts_only_lambda_service():
    props = _role_properties(_load_template())
    statements = props["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    assert statements[0]["Effect"] == "Allow"
    assert statements[0]["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert statements[0]["Action"] == "sts:AssumeRole"


# --- D. Role has only required Lambda-invoke + logging + minimal SQS poll permissions ---


def test_role_has_exactly_three_policies():
    props = _role_properties(_load_template())
    policies = props.get("Policies", [])
    assert len(policies) == 3
    policy_names = {p["PolicyName"] for p in policies}
    assert policy_names == {
        "SentinelGridSqsConsumerLogging",
        "SentinelGridSqsConsumerInvokeAnalyzer",
        "SentinelGridSqsConsumerPollQueue",
    }


def test_logging_policy_grants_exactly_the_three_log_actions_scoped_to_own_log_group():
    props = _role_properties(_load_template())
    policy = _find_policy(props, "SentinelGridSqsConsumerLogging")
    statement = policy["PolicyDocument"]["Statement"][0]
    assert set(statement["Action"]) == {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"}
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert len(resources) == 1
    assert "Fn::Sub" in resources[0]
    assert "/aws/lambda/SentinelGridSqsTelemetryConsumer:*" in resources[0]["Fn::Sub"]


# --- E. Invoke permission scoped to exact Analyzer ARN ---


def test_invoke_analyzer_policy_grants_exactly_invokefunction_scoped_to_the_analyzer_arn():
    props = _role_properties(_load_template())
    policy = _find_policy(props, "SentinelGridSqsConsumerInvokeAnalyzer")
    statement = policy["PolicyDocument"]["Statement"][0]
    assert statement["Action"] == ["lambda:InvokeFunction"]
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert resources == [{"Ref": "AnomalyDetectorFunctionArn"}]


# --- F. Minimal SQS poll permissions, scoped, no SendMessage/PurgeQueue/wildcard ---


def test_poll_queue_policy_grants_only_receive_delete_getattributes_scoped_to_the_queue_arn():
    props = _role_properties(_load_template())
    policy = _find_policy(props, "SentinelGridSqsConsumerPollQueue")
    statement = policy["PolicyDocument"]["Statement"][0]
    assert set(statement["Action"]) == {"sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"}
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert resources == [{"Ref": "TelemetryQueueArn"}]


def test_no_forbidden_sqs_or_other_service_actions_anywhere_in_template():
    raw = TEMPLATE_PATH.read_text()
    for forbidden in ("sqs:SendMessage", "sqs:PurgeQueue", "sqs:*", "lambda:*", "states:", "dynamodb:", "iam:"):
        assert forbidden not in raw, f"found forbidden permission {forbidden!r}"


def test_no_wildcard_resource_anywhere():
    raw = TEMPLATE_PATH.read_text()
    assert '"*"' not in raw
    assert not re.search(r"Resource:\s*\*\s*$", raw, re.MULTILINE)


def test_no_administrator_or_poweruser_or_managed_policies():
    template = _load_template()
    raw = TEMPLATE_PATH.read_text()
    assert "AdministratorAccess" not in raw
    assert "PowerUserAccess" not in raw
    props = _role_properties(template)
    assert "ManagedPolicyArns" not in props or props["ManagedPolicyArns"] == []


def test_function_uses_the_dedicated_role_not_an_existing_one():
    template = _load_template()
    fn_props = _function_properties(template)
    assert fn_props["Role"] == {"Fn::GetAtt": ["SqsConsumerExecutionRole", "Arn"]}

    raw = TEMPLATE_PATH.read_text()
    for existing_role in (
        "SentinelGridAnomalyDetectorExecutionRole",
        "SentinelGridRemediationTriggerExecutionRole",
        "SentinelGridRemediationPlannerRole",
        "SentinelGridRemediationExecutorRole",
        "SentinelGridRemediationVerifierRole",
        "SentinelGridRemediationWorkflowRole",
    ):
        assert existing_role not in raw


# --- G. Event source mapping targets exact queue ARN and exact consumer Lambda ---


def test_event_source_mapping_targets_the_queue_and_this_function():
    template = _load_template()
    mapping = _mapping_properties(template)
    assert mapping["EventSourceArn"] == {"Ref": "TelemetryQueueArn"}
    assert mapping["FunctionName"] == {"Ref": "SqsConsumerFunction"}


# --- H. Batch failure reporting enabled ---


def test_event_source_mapping_reports_batch_item_failures():
    mapping = _mapping_properties(_load_template())
    assert mapping["FunctionResponseTypes"] == ["ReportBatchItemFailures"]


# --- I. Event source mapping enabled ---


def test_event_source_mapping_is_enabled():
    mapping = _mapping_properties(_load_template())
    assert mapping["Enabled"] is True


def test_batch_size_parameter_is_conservative():
    template = _load_template()
    param = template["Parameters"]["BatchSize"]
    assert int(param["Default"]) <= 10
    mapping = _mapping_properties(template)
    assert mapping["BatchSize"] == {"Ref": "BatchSize"}


# --- J. No Step Functions permission ---


def test_no_step_functions_permission():
    raw = TEMPLATE_PATH.read_text()
    assert "states:" not in raw
    assert "stepfunctions" not in raw.lower()


# --- K. No DynamoDB permission ---


def test_no_dynamodb_permission():
    raw = TEMPLATE_PATH.read_text()
    assert "dynamodb:" not in raw


# --- L. No hardcoded account-specific ARN, S3 pinning present ---


def test_no_hardcoded_account_specific_arn():
    raw = TEMPLATE_PATH.read_text()
    assert not re.search(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:", raw)


def test_s3_deployment_parameters_and_version_pinning_present():
    template = _load_template()
    params = template["Parameters"]

    assert "DeploymentBucketName" in params
    assert "Default" not in params["DeploymentBucketName"]
    assert "DeploymentPackageKey" in params
    assert "Default" in params["DeploymentPackageKey"]
    assert "DeploymentPackageVersion" in params
    assert "Default" not in params["DeploymentPackageVersion"]

    code = _function_properties(template)["Code"]
    assert code["S3Bucket"] == {"Ref": "DeploymentBucketName"}
    assert code["S3Key"] == {"Ref": "DeploymentPackageKey"}
    assert code["S3ObjectVersion"] == {"Ref": "DeploymentPackageVersion"}


def test_environment_variable_references_analyzer_arn_parameter():
    template = _load_template()
    props = _function_properties(template)
    env_vars = props["Environment"]["Variables"]
    assert env_vars["ANOMALY_DETECTOR_FUNCTION_ARN"] == {"Ref": "AnomalyDetectorFunctionArn"}


def test_top_level_description_within_limit():
    template = _load_template()
    assert len(template["Description"]) <= 1024


def test_function_description_within_lambda_limit():
    props = _function_properties(_load_template())
    assert len(props["Description"]) <= 256


def test_only_expected_resource_types_are_created():
    template = _load_template()
    actual_types = {r["Type"] for r in template["Resources"].values()}
    assert actual_types == {"AWS::IAM::Role", "AWS::Lambda::Function", "AWS::Lambda::EventSourceMapping"}


# --- M. Existing remediation infrastructure not modified/referenced ---


def test_template_does_not_reference_unrelated_existing_infrastructure():
    raw = TEMPLATE_PATH.read_text()
    for name in (
        "SentinelGridAnomalyEvents",
        "sentinelgrid_telemetry_to_lambda",
        "sentinelgrid_telemetry_to_dynamodb",
        "SentinelGridRemediationPlanner",
        "SentinelGridRemediationExecutor",
        "SentinelGridRemediationVerifier",
        "SentinelGridRemediationWorkflow",
    ):
        assert name not in raw


def test_deploy_script_reads_dependent_arns_live_not_hardcoded():
    script = DEPLOY_SCRIPT.read_text()
    assert "ANOMALY_DETECTOR_STACK_NAME" in script
    assert "SQS_QUEUE_STACK_NAME" in script
    assert "FunctionArn" in script
    assert "QueueArn" in script
    assert 'STACK_NAME="${STACK_NAME:-sentinelgrid-sqs-consumer}"' in script
    assert 'AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"' in script
    assert 'AWS_REGION="${AWS_REGION:-ap-south-1}"' in script
    assert "CAPABILITY_NAMED_IAM" in script


def test_deploy_script_does_not_modify_dependent_stacks():
    script = DEPLOY_SCRIPT.read_text()
    assert script.count("cloudformation deploy") == 1  # only the new sqs-consumer stack is ever deployed


def test_deploy_script_does_not_touch_the_direct_iot_rule():
    script = DEPLOY_SCRIPT.read_text()
    # The script's own explanatory comments and echoed notices mention
    # the direct rule (to document that disabling it is a deliberate,
    # separate step) without actually deploying or modifying it — so
    # only non-executing lines (`#` comments, `echo` notices) are
    # allowed; any reference inside an actual AWS CLI invocation/argument
    # would mean the script actually acts on that template/rule.
    for line in script.splitlines():
        stripped = line.strip()
        if "anomaly-detection-rule.yaml" in line or "sentinelgrid_telemetry_to_lambda" in line:
            assert stripped.startswith("#") or stripped.startswith("echo"), (
                f"non-comment, non-echo reference to the direct IoT rule: {line!r}"
            )
    assert script.count("cloudformation deploy") == 1  # only the new sqs-consumer stack is ever deployed


# --- N. Cutover: the direct IoT rule is disabled, not deleted ---


def test_direct_iot_rule_is_disabled_not_deleted():
    raw = RULE_TEMPLATE_PATH.read_text()
    template = load_cfn_template(RULE_TEMPLATE_PATH)
    rule = _resources_of_type(template, "AWS::IoT::TopicRule")
    assert len(rule) == 1, "the direct IoT -> Analyzer rule resource must still exist (disabled, not deleted)"
    assert rule[0]["Properties"]["TopicRulePayload"]["RuleDisabled"] is True
    assert rule[0]["Properties"]["RuleName"] == "sentinelgrid_telemetry_to_lambda"
    # The Lambda action itself must still be present (a preserved,
    # historical record of the direct path) — only RuleDisabled changed.
    assert "Lambda" in str(rule[0]["Properties"]["TopicRulePayload"]["Actions"])
    # Exactly one actual property assignment ("RuleDisabled:") — the word
    # may also appear in the template's own explanatory Description text,
    # which this count deliberately excludes.
    assert raw.count("RuleDisabled:") == 1


# ============================================================================
# PACKAGING TESTS
# ============================================================================


def _build_package(output_zip: Path):
    subprocess.run(
        [str(PACKAGE_SCRIPT), str(output_zip)],
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _zip_entries(zip_path: Path) -> set:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


def test_package_script_exists():
    assert PACKAGE_SCRIPT.exists()


def test_zip_contains_handler_and_required_modules():
    with tempfile.TemporaryDirectory() as tmp:
        output_zip = Path(tmp) / "sqs-consumer.zip"
        _build_package(output_zip)
        entries = _zip_entries(output_zip)

        missing = REQUIRED_ZIP_ENTRIES - entries
        assert not missing, f"Missing required module(s): {missing}"


def test_zip_does_not_bundle_the_anomaly_detector_package():
    with tempfile.TemporaryDirectory() as tmp:
        output_zip = Path(tmp) / "sqs-consumer.zip"
        _build_package(output_zip)
        entries = _zip_entries(output_zip)

        offending = [e for e in entries if "anomaly_detector/" in e]
        assert not offending, f"zip unexpectedly bundles the Analyzer package: {offending}"


def test_zip_excludes_forbidden_directories():
    with tempfile.TemporaryDirectory() as tmp:
        output_zip = Path(tmp) / "sqs-consumer.zip"
        _build_package(output_zip)
        entries = _zip_entries(output_zip)

        for forbidden in FORBIDDEN_ZIP_SUBSTRINGS:
            offending = [e for e in entries if forbidden in e]
            assert not offending, f"zip unexpectedly contains: {offending}"


def test_no_credentials_or_secrets_in_packaged_source():
    with tempfile.TemporaryDirectory() as tmp:
        output_zip = Path(tmp) / "sqs-consumer.zip"
        _build_package(output_zip)

        secret_pattern = re.compile(r"AKIA[0-9A-Z]{16}|BEGIN[ A-Z]*PRIVATE KEY|aws_secret_access_key", re.IGNORECASE)
        with zipfile.ZipFile(output_zip) as zf:
            for entry in zf.namelist():
                if entry.endswith(".py"):
                    content = zf.read(entry).decode("utf-8", errors="replace")
                    assert not secret_pattern.search(content), f"possible secret found in {entry}"


def test_handler_imports_successfully_in_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        output_zip = Path(tmp) / "sqs-consumer.zip"
        _build_package(output_zip)

        with tempfile.TemporaryDirectory() as isolated_cwd:
            code = (
                f"import sys; sys.path.insert(0, {str(output_zip)!r}); "
                "import importlib; m = importlib.import_module('lambda_functions.sqs_consumer.handler'); "
                "assert hasattr(m, 'lambda_handler')"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=isolated_cwd,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"isolated import failed:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_packaging_is_reproducible_same_content_each_run():
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        zip1 = Path(tmp1) / "sqs-consumer.zip"
        zip2 = Path(tmp2) / "sqs-consumer.zip"
        _build_package(zip1)
        _build_package(zip2)
        assert _zip_entries(zip1) == _zip_entries(zip2)
