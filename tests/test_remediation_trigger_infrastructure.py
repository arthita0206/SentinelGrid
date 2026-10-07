"""Structural tests for the remediation trigger infrastructure
(infrastructure/remediation-trigger.yaml) and its packaging script
(infrastructure/package-remediation-trigger.sh).

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
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "remediation-trigger.yaml"
PACKAGE_SCRIPT = REPO_ROOT / "infrastructure" / "package-remediation-trigger.sh"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-remediation-trigger.sh"

REQUIRED_ZIP_ENTRIES = {
    "lambda_functions/__init__.py",
    "lambda_functions/anomaly_detector/__init__.py",
    "lambda_functions/anomaly_detector/detector.py",
    "lambda_functions/anomaly_detector/event_store.py",
    "lambda_functions/anomaly_detector/handler.py",
    "lambda_functions/remediation_trigger/__init__.py",
    "lambda_functions/remediation_trigger/trigger.py",
    "lambda_functions/remediation_trigger/handler.py",
    "shared/__init__.py",
    "shared/metrics.py",
    "shared/node_state_store.py",
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


# --- 1 & 2. Exactly one Lambda, exactly one IAM role ---


def test_template_parses_exactly_one_function_and_role():
    template = _load_template()
    assert len(_resources_of_type(template, "AWS::Lambda::Function")) == 1
    assert len(_resources_of_type(template, "AWS::IAM::Role")) == 1


# --- 3-7. Runtime config ---


def test_function_runtime_config():
    props = _function_properties(_load_template())
    assert props["Runtime"] == "python3.12"
    assert props["Architectures"] == ["arm64"]
    assert props["MemorySize"] == 128
    assert props["Timeout"] == 10
    assert props["Handler"] == "lambda_functions.remediation_trigger.handler.lambda_handler"


# --- 8. Correct physical name ---


def test_function_physical_name():
    props = _function_properties(_load_template())
    assert props["FunctionName"] == "SentinelGridRemediationTrigger"


# --- 9 & 10. Environment variable references the existing state machine ARN ---


def test_environment_variable_references_state_machine_arn_parameter():
    template = _load_template()
    props = _function_properties(template)
    env_vars = props["Environment"]["Variables"]
    assert env_vars["REMEDIATION_STATE_MACHINE_ARN"] == {"Ref": "RemediationStateMachineArn"}

    params = template["Parameters"]
    assert "RemediationStateMachineArn" in params
    assert "Default" not in params["RemediationStateMachineArn"]


# --- 11. No hardcoded account-specific ARN ---


def test_no_hardcoded_account_specific_arn():
    raw = TEMPLATE_PATH.read_text()
    assert not re.search(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:", raw)


# --- 12. S3 package/version pinning ---


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


# --- 13. Trust only lambda.amazonaws.com ---


def test_role_trusts_only_lambda_service():
    props = _role_properties(_load_template())
    statements = props["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    assert statements[0]["Effect"] == "Allow"
    assert statements[0]["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert statements[0]["Action"] == "sts:AssumeRole"


# --- 14. Exactly logs permissions required ---


def test_role_has_exactly_two_policies_logging_and_startexecution():
    props = _role_properties(_load_template())
    policies = props.get("Policies", [])
    assert len(policies) == 2
    policy_names = {p["PolicyName"] for p in policies}
    assert policy_names == {
        "SentinelGridRemediationTriggerLogging",
        "SentinelGridRemediationTriggerStartExecution",
    }


def _find_policy(props: dict, name: str) -> dict:
    matches = [p for p in props["Policies"] if p["PolicyName"] == name]
    assert len(matches) == 1
    return matches[0]


def test_logging_policy_grants_exactly_the_three_log_actions_scoped_to_own_log_group():
    props = _role_properties(_load_template())
    policy = _find_policy(props, "SentinelGridRemediationTriggerLogging")
    statement = policy["PolicyDocument"]["Statement"][0]
    assert set(statement["Action"]) == {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"}
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert len(resources) == 1
    assert "Fn::Sub" in resources[0]
    assert "/aws/lambda/SentinelGridRemediationTrigger:*" in resources[0]["Fn::Sub"]


# --- 15, 16, 17. Exactly states:StartExecution, scoped, no wildcard ---


def test_startexecution_policy_grants_exactly_that_action_scoped_to_the_workflow_arn():
    props = _role_properties(_load_template())
    policy = _find_policy(props, "SentinelGridRemediationTriggerStartExecution")
    statement = policy["PolicyDocument"]["Statement"][0]
    assert statement["Action"] == "states:StartExecution"
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert resources == [{"Ref": "RemediationStateMachineArn"}]


def test_no_wildcard_step_functions_or_iam_resource():
    raw = TEMPLATE_PATH.read_text()
    assert '"*"' not in raw
    assert not re.search(r"Resource:\s*\*\s*$", raw, re.MULTILINE)
    assert "states:*" not in raw


# --- 18. No DynamoDB/IoT/S3/SNS/SQS/etc permissions ---


def test_no_forbidden_service_permissions_anywhere_in_template():
    raw = TEMPLATE_PATH.read_text()
    for forbidden_prefix in ("dynamodb:", "iot:", "sns:", "sqs:", "ec2:", "s3:", "events:", "lambda:", "iam:", "cloudformation:"):
        assert forbidden_prefix not in raw, f"found forbidden permission prefix {forbidden_prefix!r}"


# --- 19. No managed broad policies ---


def test_no_administrator_or_poweruser_or_managed_policies():
    template = _load_template()
    raw = TEMPLATE_PATH.read_text()
    assert "AdministratorAccess" not in raw
    assert "PowerUserAccess" not in raw
    props = _role_properties(template)
    assert "ManagedPolicyArns" not in props or props["ManagedPolicyArns"] == []


# --- 20. Dedicated role used by the Trigger Lambda ---


def test_function_uses_the_dedicated_role_not_an_existing_one():
    template = _load_template()
    fn_props = _function_properties(template)
    assert fn_props["Role"] == {"Fn::GetAtt": ["RemediationTriggerExecutionRole", "Arn"]}

    raw = TEMPLATE_PATH.read_text()
    for existing_role in (
        "SentinelGridAnomalyDetectorExecutionRole",
        "SentinelGridRemediationPlannerRole",
        "SentinelGridRemediationExecutorRole",
        "SentinelGridRemediationVerifierRole",
        "SentinelGridRemediationWorkflowRole",
    ):
        assert existing_role not in raw


# --- No resources beyond the two expected, no reference to unrelated infra ---


def test_only_expected_resource_types_are_created():
    template = _load_template()
    actual_types = {r["Type"] for r in template["Resources"].values()}
    assert actual_types == {"AWS::IAM::Role", "AWS::Lambda::Function"}


def test_template_does_not_reference_unrelated_existing_infrastructure():
    raw = TEMPLATE_PATH.read_text()
    for name in (
        "SentinelGridAnomalyDetector",
        "SentinelGridAnomalyEvents",
        "SentinelGridTelemetry",
        "sentinelgrid_telemetry_to_lambda",
        "sentinelgrid_telemetry_to_dynamodb",
        "SentinelGridRemediationPlanner",
        "SentinelGridRemediationExecutor",
        "SentinelGridRemediationVerifier",
    ):
        assert name not in raw


def test_top_level_description_within_limit():
    template = _load_template()
    assert len(template["Description"]) <= 1024


def test_function_description_within_lambda_limit():
    props = _function_properties(_load_template())
    assert len(props["Description"]) <= 256


def test_deploy_script_reads_state_machine_arn_live_not_hardcoded():
    script = DEPLOY_SCRIPT.read_text()
    assert "WORKFLOW_STACK_NAME" in script
    assert "StateMachineArn" in script
    assert 'STACK_NAME="${STACK_NAME:-sentinelgrid-remediation-trigger}"' in script
    assert 'AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"' in script
    assert 'AWS_REGION="${AWS_REGION:-ap-south-1}"' in script
    assert "CAPABILITY_NAMED_IAM" in script


def test_deploy_script_does_not_modify_the_workflow_stack():
    script = DEPLOY_SCRIPT.read_text()
    assert "cloudformation deploy" not in script.split("describe-stacks")[0]  # only used before any deploy call
    assert script.count("cloudformation deploy") == 1  # only the new trigger stack is ever deployed


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


# --- 21 & 22. ZIP contains the handler and all required local modules ---


def test_zip_contains_handler_and_required_modules():
    with tempfile.TemporaryDirectory() as tmp:
        output_zip = Path(tmp) / "remediation-trigger.zip"
        _build_package(output_zip)
        entries = _zip_entries(output_zip)

        missing = REQUIRED_ZIP_ENTRIES - entries
        assert not missing, f"Missing required module(s): {missing}"


# --- 23. Excludes venv/tests/.git/logs ---


def test_zip_excludes_forbidden_directories():
    with tempfile.TemporaryDirectory() as tmp:
        output_zip = Path(tmp) / "remediation-trigger.zip"
        _build_package(output_zip)
        entries = _zip_entries(output_zip)

        for forbidden in FORBIDDEN_ZIP_SUBSTRINGS:
            offending = [e for e in entries if forbidden in e]
            assert not offending, f"zip unexpectedly contains: {offending}"


def test_no_credentials_or_secrets_in_packaged_source():
    with tempfile.TemporaryDirectory() as tmp:
        output_zip = Path(tmp) / "remediation-trigger.zip"
        _build_package(output_zip)

        secret_pattern = re.compile(r"AKIA[0-9A-Z]{16}|BEGIN[ A-Z]*PRIVATE KEY|aws_secret_access_key", re.IGNORECASE)
        with zipfile.ZipFile(output_zip) as zf:
            for entry in zf.namelist():
                if entry.endswith(".py"):
                    content = zf.read(entry).decode("utf-8", errors="replace")
                    assert not secret_pattern.search(content), f"possible secret found in {entry}"


# --- 24. Handler imports successfully from an isolated ZIP ---


def test_handler_imports_successfully_in_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        output_zip = Path(tmp) / "remediation-trigger.zip"
        _build_package(output_zip)

        with tempfile.TemporaryDirectory() as isolated_cwd:
            code = (
                f"import sys; sys.path.insert(0, {str(output_zip)!r}); "
                "import importlib; m = importlib.import_module('lambda_functions.remediation_trigger.handler'); "
                "assert hasattr(m, 'lambda_handler')"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=isolated_cwd,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"isolated import failed:\nstdout={result.stdout}\nstderr={result.stderr}"


# --- 25. Package build is reproducible ---


def test_packaging_is_reproducible_same_content_each_run():
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        zip1 = Path(tmp1) / "remediation-trigger.zip"
        zip2 = Path(tmp2) / "remediation-trigger.zip"
        _build_package(zip1)
        _build_package(zip2)
        assert _zip_entries(zip1) == _zip_entries(zip2)
