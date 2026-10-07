"""Structural tests for the anomaly detector's Lambda infrastructure
(infrastructure/anomaly-detector.yaml) and its packaging script
(infrastructure/package-anomaly-detector.sh).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no S3/Lambda access, no credentials.
The packaging test does invoke the local shell script, but that script
only copies local files and runs `zip` — it makes no network calls.
"""

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "anomaly-detector.yaml"
STORAGE_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "anomaly-events-storage.yaml"
PACKAGE_SCRIPT = REPO_ROOT / "infrastructure" / "package-anomaly-detector.sh"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-anomaly-detector.sh"


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _find_policy_by_name(role: dict, policy_name: str) -> dict:
    matches = [p for p in role["Properties"].get("Policies", []) if p["PolicyName"] == policy_name]
    assert len(matches) == 1, f"expected exactly one policy named {policy_name!r}, found {len(matches)}"
    return matches[0]


# --- Template structure ---


def test_template_file_exists():
    assert TEMPLATE_PATH.exists()


def test_template_parses_and_has_expected_top_level_sections():
    template = _load_template()
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Resources" in template
    assert "Parameters" in template


def test_deployment_bucket_parameter_has_no_default():
    # Must be supplied explicitly at deploy time — this template must
    # never silently invent or assume a bucket.
    params = _load_template()["Parameters"]
    assert "DeploymentBucketName" in params
    assert "Default" not in params["DeploymentBucketName"]


# --- Lambda function ---


def _find_resource_by_type(resources: dict, resource_type: str) -> dict:
    matches = [r for r in resources.values() if r.get("Type") == resource_type]
    assert len(matches) == 1, f"expected exactly one {resource_type}, found {len(matches)}"
    return matches[0]


def test_lambda_function_is_defined_with_expected_name():
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    assert function["Properties"]["FunctionName"] == "SentinelGridAnomalyDetector"


def test_lambda_handler_points_to_the_existing_adapter():
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    assert (
        function["Properties"]["Handler"]
        == "lambda_functions.anomaly_detector.handler.lambda_handler"
    )


def test_lambda_runtime_is_a_currently_supported_python312():
    # Deliberately NOT derived from .python-version (local dev stays on
    # 3.9): AWS Lambda's python3.9 runtime is deprecated, so the deployed
    # runtime is pinned independently to python3.12, a currently supported
    # runtime with a long remaining support lifetime. The detector/handler
    # use only standard-library features, so they are expected to be
    # compatible without any code changes.
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    assert function["Properties"]["Runtime"] == "python3.12"


def test_lambda_has_no_vpc_configuration():
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    assert "VpcConfig" not in function["Properties"]


def test_lambda_code_comes_from_s3_not_inline():
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    code = function["Properties"]["Code"]

    assert "ZipFile" not in code
    assert code["S3Bucket"] == {"Ref": "DeploymentBucketName"}
    assert code["S3Key"] == {"Ref": "DeploymentPackageKey"}


def test_lambda_code_pins_an_s3_object_version_from_the_parameter():
    # Overwriting the same S3 key in place doesn't change CloudFormation's
    # view of the resource — pinning to the upload's own VersionId is what
    # makes a redeploy after a content change actually take effect.
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    code = function["Properties"]["Code"]

    assert "S3ObjectVersion" in code
    assert code["S3ObjectVersion"] == {"Ref": "DeploymentPackageVersion"}
    # Must come from the parameter, not a literal/hardcoded version id.
    assert not isinstance(code["S3ObjectVersion"], str)


def test_deployment_package_version_parameter_exists_and_is_required():
    params = _load_template()["Parameters"]
    assert "DeploymentPackageVersion" in params
    assert "Default" not in params["DeploymentPackageVersion"]
    assert params["DeploymentPackageVersion"]["Type"] == "String"


def test_no_hardcoded_s3_object_version_anywhere_in_template():
    # A real S3 VersionId is a long opaque token (AWS-generated); the
    # template should only ever reference it via the parameter, never as
    # a literal string.
    raw_text = TEMPLATE_PATH.read_text()
    assert "S3ObjectVersion: !Ref DeploymentPackageVersion" in raw_text
    for line in raw_text.splitlines():
        if "S3ObjectVersion" in line:
            assert "!Ref" in line, f"S3ObjectVersion must be a !Ref, found: {line!r}"


def test_lambda_has_reasonable_timeout_and_small_memory():
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    props = function["Properties"]

    assert 0 < props["Timeout"] <= 30
    assert props["MemorySize"] <= 256


# --- IAM execution role ---


def test_execution_role_is_present_and_trusts_only_lambda():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Effect"] == "Allow"
    assert statement["Principal"]["Service"] == "lambda.amazonaws.com"
    assert statement["Action"] == "sts:AssumeRole"


def test_execution_role_grants_only_basic_cloudwatch_logs_permissions():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    # Five policies: logging (unchanged), the anomaly-events write grant
    # (see the dynamodb-specific tests below), (Step 22) the
    # remediation-trigger invoke grant (see the lambda-specific tests
    # further down this file), (Step 28) the namespace-scoped CloudWatch
    # metrics publish grant (see the metrics-specific tests further down
    # this file), and (Step 34) the node-state UpdateItem grant.
    assert len(role["Properties"].get("Policies", [])) == 5

    logging_policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorLogging")
    statements = logging_policy["PolicyDocument"]["Statement"]
    assert len(statements) == 1
    statement = statements[0]

    actions = statement["Action"]
    actions = actions if isinstance(actions, list) else [actions]
    assert set(actions) == {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"}


def test_execution_role_grants_exactly_dynamodb_putitem_on_anomaly_events_table():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    write_policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorAnomalyEventsWrite")
    statements = write_policy["PolicyDocument"]["Statement"]
    assert len(statements) == 1
    statement = statements[0]

    actions = statement["Action"]
    actions = actions if isinstance(actions, list) else [actions]
    assert actions == ["dynamodb:PutItem"]

    forbidden_actions = (
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    )
    assert not any(forbidden in actions for forbidden in forbidden_actions)


def test_dynamodb_permission_resource_is_the_imported_table_arn_not_wildcard():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")
    write_policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorAnomalyEventsWrite")
    statement = write_policy["PolicyDocument"]["Statement"][0]

    resource = statement["Resource"]
    resource = resource if isinstance(resource, list) else [resource]

    assert resource == [{"Fn::ImportValue": "SentinelGridAnomalyEventsTableArn"}]
    assert "*" not in resource


def test_no_dynamodb_action_uses_wildcard_resource_anywhere_in_role():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    for policy in role["Properties"].get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if any(action.lower().startswith("dynamodb:") for action in actions):
                resource = statement["Resource"]
                resource = resource if isinstance(resource, list) else [resource]
                assert "*" not in resource


def test_exactly_two_dynamodb_statements_exist_in_the_whole_role():
    """Step 34 adds a second DynamoDB statement (node-state UpdateItem)
    alongside the existing (Step 9/17) anomaly-events PutItem grant —
    this replaces the old "exactly one" guard with an "exactly two,
    nothing broader" guard.
    """
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    dynamodb_statements = []
    for policy in role["Properties"].get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if any(action.lower().startswith("dynamodb:") for action in actions):
                dynamodb_statements.append(statement)

    assert len(dynamodb_statements) == 2
    all_actions = {a for stmt in dynamodb_statements for a in stmt["Action"]}
    assert all_actions == {"dynamodb:PutItem", "dynamodb:UpdateItem"}


def test_node_state_write_policy_is_least_privilege():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")
    policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorNodeStateWrite")
    statement = policy["PolicyDocument"]["Statement"][0]

    assert statement["Action"] == ["dynamodb:UpdateItem"]
    assert statement["Resource"] == [{"Fn::ImportValue": "SentinelGridNodeStateTableArn"}]


# --- Step 22: lambda:InvokeFunction on the remediation trigger ---


def test_execution_role_grants_exactly_lambda_invokefunction_on_the_trigger():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    invoke_policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorInvokeRemediationTrigger")
    statements = invoke_policy["PolicyDocument"]["Statement"]
    assert len(statements) == 1
    statement = statements[0]

    actions = statement["Action"]
    actions = actions if isinstance(actions, list) else [actions]
    assert actions == ["lambda:InvokeFunction"]


def test_lambda_invoke_resource_is_exactly_the_trigger_arn_parameter_not_wildcard():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")
    invoke_policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorInvokeRemediationTrigger")
    statement = invoke_policy["PolicyDocument"]["Statement"][0]

    resource = statement["Resource"]
    resource = resource if isinstance(resource, list) else [resource]

    assert resource == [{"Ref": "RemediationTriggerFunctionArn"}]
    assert "*" not in resource


def test_no_lambda_action_uses_wildcard_resource_anywhere_in_role():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    for policy in role["Properties"].get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if any(action.lower().startswith("lambda:") for action in actions):
                assert actions == ["lambda:InvokeFunction"]
                resource = statement["Resource"]
                resource = resource if isinstance(resource, list) else [resource]
                assert "*" not in resource


def test_publish_metrics_policy_grants_exactly_putmetricdata_scoped_by_namespace_condition():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")
    metrics_policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorPublishMetrics")
    statement = metrics_policy["PolicyDocument"]["Statement"][0]

    actions = statement["Action"]
    actions = actions if isinstance(actions, list) else [actions]
    assert actions == ["cloudwatch:PutMetricData"]

    # PutMetricData has no ARN-level resource scoping -- Resource: "*" is
    # required by the action itself. The real authorization boundary is
    # the cloudwatch:namespace condition below.
    assert statement["Resource"] == "*"
    assert statement["Condition"] == {"StringEquals": {"cloudwatch:namespace": "SentinelGrid"}}


def test_no_cloudwatch_action_beyond_putmetricdata_anywhere_in_role():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    for policy in role["Properties"].get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            cloudwatch_actions = [a for a in actions if a.lower().startswith("cloudwatch:") and not a.lower().startswith("cloudwatch:logs")]
            if cloudwatch_actions:
                assert cloudwatch_actions == ["cloudwatch:PutMetricData"]


def test_only_one_lambda_statement_exists_in_the_whole_role():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    lambda_statements = []
    for policy in role["Properties"].get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if any(action.lower().startswith("lambda:") for action in actions):
                lambda_statements.append(statement)

    assert len(lambda_statements) == 1


def test_no_step_functions_permission_was_added():
    raw = TEMPLATE_PATH.read_text()
    assert "states:" not in raw


def test_dynamodb_permissions_remain_intact_after_step_22():
    # Regression guard: adding the new lambda:InvokeFunction policy must
    # not have disturbed the existing, already-verified DynamoDB grant.
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")
    write_policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorAnomalyEventsWrite")
    statement = write_policy["PolicyDocument"]["Statement"][0]
    assert statement["Action"] == ["dynamodb:PutItem"]
    assert statement["Resource"] == [{"Fn::ImportValue": "SentinelGridAnomalyEventsTableArn"}]


def test_trigger_arn_supplied_through_parameter_not_hardcoded():
    template = _load_template()
    params = template["Parameters"]
    assert "RemediationTriggerFunctionArn" in params
    assert "Default" not in params["RemediationTriggerFunctionArn"]

    raw = TEMPLATE_PATH.read_text()
    assert not re.search(r"arn:aws:lambda:[a-z0-9-]+:\d{12}:function:SentinelGridRemediationTrigger", raw)


def test_environment_variable_references_the_trigger_arn_parameter():
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    env_vars = function["Properties"]["Environment"]["Variables"]
    assert env_vars["REMEDIATION_TRIGGER_FUNCTION_ARN"] == {"Ref": "RemediationTriggerFunctionArn"}


def test_environment_variable_references_the_node_state_table_name_export():
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    env_vars = function["Properties"]["Environment"]["Variables"]
    assert env_vars["NODE_STATE_TABLE_NAME"] == {"Fn::ImportValue": "SentinelGridNodeStateTableName"}
    assert set(env_vars.keys()) == {"REMEDIATION_TRIGGER_FUNCTION_ARN", "NODE_STATE_TABLE_NAME"}


def test_no_second_execution_role_is_created():
    resources = _load_template()["Resources"]
    roles = [r for r in resources.values() if r.get("Type") == "AWS::IAM::Role"]

    assert len(roles) == 1
    assert roles[0]["Properties"]["RoleName"] == "SentinelGridAnomalyDetectorExecutionRole"


def test_import_value_name_matches_the_storage_stacks_export_name():
    # Cross-stack contract: the string used in !ImportValue here must
    # exactly match the Export Name in anomaly-events-storage.yaml, or
    # this stack would fail to deploy against a real account.
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")
    write_policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorAnomalyEventsWrite")
    imported_name = write_policy["PolicyDocument"]["Statement"][0]["Resource"][0]["Fn::ImportValue"]

    storage_template = load_cfn_template(STORAGE_TEMPLATE_PATH)
    exported_name = storage_template["Outputs"]["AnomalyEventsTableArn"]["Export"]["Name"]

    assert imported_name == exported_name == "SentinelGridAnomalyEventsTableArn"


def test_execution_role_has_no_iot_or_stepfunctions_or_sns_permissions():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    forbidden_prefixes = ("iot:", "states:", "sns:")
    for policy in role["Properties"].get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            for action in actions:
                assert not any(action.lower().startswith(p) for p in forbidden_prefixes)


def test_no_administrator_access_or_wildcard_action_granted():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    managed_arns = role["Properties"].get("ManagedPolicyArns", [])
    assert not any("AdministratorAccess" in arn for arn in managed_arns)

    for policy in role["Properties"].get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            assert "*" not in actions


def test_cloudwatch_logs_resource_is_scoped_not_wildcard():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")
    logging_policy = _find_policy_by_name(role, "SentinelGridAnomalyDetectorLogging")
    statement = logging_policy["PolicyDocument"]["Statement"][0]

    resource = statement["Resource"]
    resource = resource if isinstance(resource, list) else [resource]
    for entry in resource:
        assert entry != "*"


def test_lambda_role_reference_matches_execution_role():
    resources = _load_template()["Resources"]
    function = _find_resource_by_type(resources, "AWS::Lambda::Function")
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    role_logical_id = [k for k, v in resources.items() if v is role][0]
    assert function["Properties"]["Role"] == {"Fn::GetAtt": [role_logical_id, "Arn"]}


def test_no_hardcoded_credentials_or_account_ids_in_template():
    raw_text = TEMPLATE_PATH.read_text().lower()
    for forbidden in ("akia", "secret", "access_key", "accesskey", "password", "arn:aws:iam::123456789012"):
        assert forbidden not in raw_text


# --- Scripts: no credentials, fail clearly, no auto-deploy ---


def test_scripts_exist_and_are_executable():
    for script in (PACKAGE_SCRIPT, DEPLOY_SCRIPT):
        assert script.exists()
        assert script.stat().st_mode & 0o111, f"{script} is not executable"


def test_scripts_contain_no_hardcoded_credentials():
    for script in (PACKAGE_SCRIPT, DEPLOY_SCRIPT):
        raw_text = script.read_text().lower()
        for forbidden in ("akia", "aws_secret_access_key=", "aws_access_key_id="):
            assert forbidden not in raw_text


def test_deploy_script_requires_deployment_bucket_and_does_not_invent_one():
    text = DEPLOY_SCRIPT.read_text()
    assert "DEPLOYMENT_BUCKET" in text
    assert "aws s3 mb" not in text  # never silently creates a bucket
    assert "CreateBucket" not in text


def test_deploy_script_verifies_bucket_exists():
    text = DEPLOY_SCRIPT.read_text()
    assert "head-bucket" in text


def test_deploy_script_verifies_versioning_and_fails_clearly_if_disabled():
    text = DEPLOY_SCRIPT.read_text()
    assert "get-bucket-versioning" in text
    # Must actually branch on the status and exit non-zero rather than
    # silently proceeding with an unversioned object.
    assert '"${VERSIONING_STATUS}" != "Enabled"' in text
    assert "exit 1" in text
    # It documents the fix (enabling versioning) without doing it itself.
    assert "put-bucket-versioning" in text  # shown to the user, not silently run
    assert "does NOT enable versioning" in text


def test_deploy_script_retrieves_and_passes_object_version():
    text = DEPLOY_SCRIPT.read_text()
    assert "head-object" in text
    assert "VersionId" in text
    assert "PACKAGE_VERSION_ID" in text
    assert "DeploymentPackageVersion=${PACKAGE_VERSION_ID}" in text


# --- Packaging: local, offline (no AWS calls — only cp/zip) ---


def test_package_script_produces_expected_contents(tmp_path):
    output_zip = tmp_path / "anomaly-detector.zip"
    subprocess.run(
        [str(PACKAGE_SCRIPT), str(output_zip)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert output_zip.exists()

    with zipfile.ZipFile(output_zip) as zf:
        names = set(zf.namelist())

    expected_files = {
        "lambda_functions/__init__.py",
        "lambda_functions/anomaly_detector/__init__.py",
        "lambda_functions/anomaly_detector/detector.py",
        "lambda_functions/anomaly_detector/handler.py",
        "lambda_functions/anomaly_detector/event_store.py",
        "lambda_functions/anomaly_detector/remediation_trigger_client.py",
        "shared/__init__.py",
        "shared/config.py",
        "shared/telemetry.py",
        "shared/utils.py",
        "shared/metrics.py",
        "shared/node_state_store.py",
    }
    assert expected_files.issubset(names)

    excluded_markers = ("venv/", "tests/", "logs/", ".git/", "README.md", "examples/")
    for name in names:
        for marker in excluded_markers:
            assert marker not in name, f"{name} should not be packaged (matches {marker!r})"


def test_package_includes_event_store_module(tmp_path):
    # Dedicated regression test for a real deployed bug: handler.py
    # imports event_store.py at module scope, but the packaging script's
    # explicit file list was never updated when event_store.py was added
    # to the source tree, so it was silently missing from every built
    # zip. A live Lambda deployed from that package fails with
    # ModuleNotFoundError on every single invocation. This test exists
    # specifically so that omission can never go unnoticed again.
    output_zip = tmp_path / "anomaly-detector.zip"
    subprocess.run(
        [str(PACKAGE_SCRIPT), str(output_zip)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    with zipfile.ZipFile(output_zip) as zf:
        names = set(zf.namelist())

    assert "lambda_functions/anomaly_detector/event_store.py" in names, (
        "event_store.py is missing from the packaged Lambda zip — handler.py "
        "imports it at module scope, so a deployed Lambda without it fails "
        "with ModuleNotFoundError on every invocation."
    )


def test_package_includes_remediation_trigger_client_module(tmp_path):
    # Same class of regression this project has hit before (see
    # test_package_includes_event_store_module above): handler.py now
    # also imports remediation_trigger_client.py at module scope (Step
    # 22). Adding it to detector.py's own file list is not enough — this
    # asserts it directly so an omission here can never go unnoticed.
    output_zip = tmp_path / "anomaly-detector.zip"
    subprocess.run(
        [str(PACKAGE_SCRIPT), str(output_zip)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    with zipfile.ZipFile(output_zip) as zf:
        names = set(zf.namelist())

    assert "lambda_functions/anomaly_detector/remediation_trigger_client.py" in names, (
        "remediation_trigger_client.py is missing from the packaged Lambda zip — "
        "handler.py imports it at module scope, so a deployed Lambda without it "
        "fails with ModuleNotFoundError on every invocation."
    )


def test_packaged_modules_are_importable_from_the_built_zip(tmp_path):
    # Goes one step further than checking file presence: actually imports
    # the packaged handler module (and, transitively, event_store.py) from
    # inside the built zip, in a fresh subprocess with sys.path pointed at
    # nothing but that zip — proving the package is self-contained and
    # would not fail with ModuleNotFoundError when Lambda imports it. No
    # AWS calls are made; this is a pure local import check.
    output_zip = tmp_path / "anomaly-detector.zip"
    subprocess.run(
        [str(PACKAGE_SCRIPT), str(output_zip)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    # cwd is deliberately an empty directory OUTSIDE the repo: Python's -c
    # mode implicitly adds the current directory to sys.path, and if that
    # were left as the repo root, this import would silently succeed
    # against the real on-disk source tree — proving nothing about
    # whether the zip itself is self-contained. Isolating cwd forces the
    # import to only be satisfiable via PYTHONPATH, i.e. the zip.
    isolated_cwd = tmp_path / "isolated_cwd"
    isolated_cwd.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import lambda_functions.anomaly_detector.handler as h; "
            "import lambda_functions.anomaly_detector.event_store as e; "
            "print('OK', h.__name__, e.__name__)",
        ],
        cwd=isolated_cwd,
        env={**os.environ, "PYTHONPATH": str(output_zip)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"Importing the packaged handler failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
