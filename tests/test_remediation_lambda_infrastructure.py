"""Structural tests for the remediation Lambda infrastructure
(infrastructure/remediation-lambdas.yaml) and its packaging script
(infrastructure/package-remediation-lambdas.sh).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no S3/Lambda access, no credentials.
The packaging test does invoke the local shell script, but that script
only copies local files and runs `zip` — it makes no network calls.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "remediation-lambdas.yaml"
PACKAGE_SCRIPT = REPO_ROOT / "infrastructure" / "package-remediation-lambdas.sh"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-remediation-lambdas.sh"

FUNCTION_NAMES = {
    "PlannerFunction": "SentinelGridRemediationPlanner",
    "ExecutorFunction": "SentinelGridRemediationExecutor",
    "VerifierFunction": "SentinelGridRemediationVerifier",
}
ROLE_LOGICAL_IDS = {
    "PlannerFunction": "PlannerExecutionRole",
    "ExecutorFunction": "ExecutorExecutionRole",
    "VerifierFunction": "VerifierExecutionRole",
}

EXPECTED_HANDLER = {
    "PlannerFunction": "lambda_functions.remediation.planner.lambda_handler",
    "ExecutorFunction": "lambda_functions.remediation.executor.lambda_handler",
    "VerifierFunction": "lambda_functions.remediation.verifier.lambda_handler",
}

REQUIRED_ZIP_ENTRIES = {
    "lambda_functions/__init__.py",
    "lambda_functions/anomaly_detector/__init__.py",
    "lambda_functions/anomaly_detector/detector.py",
    "lambda_functions/anomaly_detector/event_store.py",
    "lambda_functions/anomaly_detector/handler.py",
    "lambda_functions/remediation/__init__.py",
    "lambda_functions/remediation/planner.py",
    "lambda_functions/remediation/executor.py",
    "lambda_functions/remediation/verifier.py",
    "remediation/__init__.py",
    "remediation/models.py",
    "remediation/engine.py",
    "remediation/integration.py",
    "shared/__init__.py",
    "shared/metrics.py",
    "shared/node_state_store.py",
}

FORBIDDEN_ZIP_SUBSTRINGS = ("venv/", "tests/", ".git/", "logs/")


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _resources_of_type(template: dict, resource_type: str) -> list:
    return [r for r in template["Resources"].values() if r["Type"] == resource_type]


def _role_properties(template: dict, logical_id: str) -> dict:
    return template["Resources"][logical_id]["Properties"]


def _find_policy(role_props: dict, policy_name: str) -> dict:
    matches = [p for p in role_props.get("Policies", []) if p["PolicyName"] == policy_name]
    assert len(matches) == 1, f"expected exactly one policy named {policy_name!r}, found {len(matches)}"
    return matches[0]


# --- 1. Template parses ---


def test_template_file_exists_and_parses():
    assert TEMPLATE_PATH.exists()
    template = _load_template()
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Resources" in template


# --- 2. Exactly 3 Lambda functions ---


def test_exactly_three_lambda_functions():
    template = _load_template()
    functions = _resources_of_type(template, "AWS::Lambda::Function")
    assert len(functions) == 3


# --- 3. Exactly 3 IAM roles ---


def test_exactly_three_iam_roles():
    template = _load_template()
    roles = _resources_of_type(template, "AWS::IAM::Role")
    assert len(roles) == 3


# --- 4. Runtime/architecture/memory/timeout/handler per function ---


def test_each_function_has_expected_runtime_config():
    template = _load_template()
    for logical_id, expected_name in FUNCTION_NAMES.items():
        props = template["Resources"][logical_id]["Properties"]
        assert props["Runtime"] == "python3.12", logical_id
        assert props["Architectures"] == ["arm64"], logical_id
        assert props["MemorySize"] == 128, logical_id
        assert props["Timeout"] == 10, logical_id
        assert props["Handler"] == EXPECTED_HANDLER[logical_id], logical_id


# --- 5. Correct Lambda names ---


def test_function_names_are_correct():
    template = _load_template()
    for logical_id, expected_name in FUNCTION_NAMES.items():
        assert template["Resources"][logical_id]["Properties"]["FunctionName"] == expected_name


# --- 6. Each function has a dedicated execution role ---


def test_each_function_has_its_own_dedicated_role():
    template = _load_template()
    role_arns_used = set()
    for logical_id, role_logical_id in ROLE_LOGICAL_IDS.items():
        role_ref = template["Resources"][logical_id]["Properties"]["Role"]
        assert role_ref == {"Fn::GetAtt": [role_logical_id, "Arn"]}
        role_arns_used.add(role_logical_id)
    assert len(role_arns_used) == 3  # no role reused across functions


# --- 7. Each role trusts only lambda.amazonaws.com ---


def test_each_role_trusts_only_lambda_service():
    template = _load_template()
    for role_logical_id in ROLE_LOGICAL_IDS.values():
        props = _role_properties(template, role_logical_id)
        statements = props["AssumeRolePolicyDocument"]["Statement"]
        assert len(statements) == 1
        assert statements[0]["Effect"] == "Allow"
        assert statements[0]["Principal"] == {"Service": "lambda.amazonaws.com"}
        assert statements[0]["Action"] == "sts:AssumeRole"


# --- 8. Each role grants only CloudWatch Logs + (Step 28) namespace-scoped metrics permissions ---


def test_each_role_grants_only_cloudwatch_logs_and_metrics_permissions():
    # Step 33: Planner and Executor gain one additional policy
    # (RecoveryEventsAccess) beyond Logging + PublishMetrics. Step 34:
    # all three additionally gain a NodeStateWrite policy -- Planner/
    # Executor therefore have 4, Verifier (no RecoveryEventsAccess) has 3.
    expected_policy_count = {"PlannerFunction": 4, "ExecutorFunction": 4, "VerifierFunction": 3}
    for logical_id, role_logical_id in ROLE_LOGICAL_IDS.items():
        template = _load_template()
        props = _role_properties(template, role_logical_id)
        policies = props.get("Policies", [])
        assert len(policies) == expected_policy_count[logical_id], (
            f"{role_logical_id} should have exactly {expected_policy_count[logical_id]} inline policies"
        )

        function_name = FUNCTION_NAMES[logical_id]  # e.g. SentinelGridRemediationPlanner
        logging_policy = _find_policy(props, f"{function_name}Logging")
        logging_statement = logging_policy["PolicyDocument"]["Statement"]
        assert len(logging_statement) == 1
        assert set(logging_statement[0]["Action"]) == {
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
        }

        metrics_policy_name = logging_policy["PolicyName"].replace("Logging", "PublishMetrics")
        metrics_policy = _find_policy(props, metrics_policy_name)
        metrics_statement = metrics_policy["PolicyDocument"]["Statement"]
        assert len(metrics_statement) == 1
        assert metrics_statement[0]["Action"] == ["cloudwatch:PutMetricData"]
        assert metrics_statement[0]["Resource"] == "*"
        assert metrics_statement[0]["Condition"] == {"StringEquals": {"cloudwatch:namespace": "SentinelGrid"}}


def test_planner_and_executor_recovery_events_policy_is_least_privilege():
    template = _load_template()
    for logical_id in ("PlannerFunction", "ExecutorFunction"):
        role_logical_id = ROLE_LOGICAL_IDS[logical_id]
        props = _role_properties(template, role_logical_id)
        function_name = FUNCTION_NAMES[logical_id]
        policy = _find_policy(props, f"{function_name}RecoveryEventsAccess")
        statement = policy["PolicyDocument"]["Statement"]
        assert len(statement) == 1
        assert set(statement[0]["Action"]) == {"dynamodb:PutItem", "dynamodb:GetItem"}
        assert statement[0]["Resource"] == [{"Fn::ImportValue": "SentinelGridRecoveryEventsTableArn"}]


def test_verifier_has_no_recovery_events_policy():
    template = _load_template()
    props = _role_properties(template, "VerifierExecutionRole")
    policy_names = {p["PolicyName"] for p in props.get("Policies", [])}
    assert not any("RecoveryEvents" in name for name in policy_names)


def test_all_three_roles_have_a_node_state_write_policy():
    """Step 34: Planner (RECOVERING/ESCALATION_REQUIRED), Executor
    (RECOVERED/FAULT_DETECTED/ESCALATION_REQUIRED), and Verifier
    (ESCALATION_REQUIRED backstop) all write current node state."""
    template = _load_template()
    for logical_id in ("PlannerFunction", "ExecutorFunction", "VerifierFunction"):
        role_logical_id = ROLE_LOGICAL_IDS[logical_id]
        props = _role_properties(template, role_logical_id)
        function_name = FUNCTION_NAMES[logical_id]
        policy = _find_policy(props, f"{function_name}NodeStateWrite")
        statement = policy["PolicyDocument"]["Statement"]
        assert len(statement) == 1
        assert statement[0]["Action"] == ["dynamodb:UpdateItem"]
        assert statement[0]["Resource"] == [{"Fn::ImportValue": "SentinelGridNodeStateTableArn"}]


# --- 9. No forbidden service permissions beyond the Step 33 DynamoDB grant ---


def test_no_forbidden_service_permissions_anywhere_in_template():
    raw = TEMPLATE_PATH.read_text()
    for forbidden_prefix in (
        "iot:",
        "states:",
        "sns:",
        "sqs:",
        "ec2:",
        "s3:",
        "events:",
    ):
        assert forbidden_prefix not in raw, f"found forbidden permission prefix {forbidden_prefix!r}"


def test_only_expected_dynamodb_actions_are_granted_anywhere():
    """Step 33 legitimately grants dynamodb:PutItem/GetItem to Planner and
    Executor; Step 34 legitimately grants dynamodb:UpdateItem to all
    three. This replaces the old blanket "no dynamodb:" ban with a
    precise check that nothing broader (Query/Scan/DeleteItem/dynamodb:*)
    ever appears.
    """
    template = _load_template()
    seen_dynamodb_actions = set()
    for role_logical_id in ROLE_LOGICAL_IDS.values():
        props = _role_properties(template, role_logical_id)
        for policy in props.get("Policies", []):
            for statement in policy["PolicyDocument"]["Statement"]:
                actions = statement["Action"]
                actions = actions if isinstance(actions, list) else [actions]
                seen_dynamodb_actions.update(a for a in actions if a.lower().startswith("dynamodb:"))

    assert seen_dynamodb_actions == {"dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"}


def test_no_administrator_or_poweruser_or_managed_policies():
    template = _load_template()
    raw = TEMPLATE_PATH.read_text()
    assert "AdministratorAccess" not in raw
    assert "PowerUserAccess" not in raw
    for role_logical_id in ROLE_LOGICAL_IDS.values():
        props = _role_properties(template, role_logical_id)
        assert "ManagedPolicyArns" not in props or props["ManagedPolicyArns"] == []


# --- 10. No wildcard resource permissions; log resources scoped tightly ---


def test_no_wildcard_iam_resource_and_log_resources_scoped_per_function():
    """No statement anywhere in this template may use an unconditional
    wildcard resource, with exactly ONE documented exception: CloudWatch
    `PutMetricData` (Step 28), which AWS does not support ARN-scoping at
    all — its `Resource: "*"` is paired with a `cloudwatch:namespace`
    Condition instead (see
    test_each_role_grants_only_cloudwatch_logs_and_metrics_permissions
    for that exact scoping). Every other statement's Resource must be
    scoped to something specific — never `"*"`.
    """
    template = _load_template()

    for logical_id, function_name in FUNCTION_NAMES.items():
        role_logical_id = ROLE_LOGICAL_IDS[logical_id]
        props = _role_properties(template, role_logical_id)
        for policy in props.get("Policies", []):
            for statement in policy["PolicyDocument"]["Statement"]:
                actions = statement["Action"]
                actions = actions if isinstance(actions, list) else [actions]
                resources = statement["Resource"]
                resources = resources if isinstance(resources, list) else [resources]

                if resources == ["*"] or resources == "*":
                    assert actions == ["cloudwatch:PutMetricData"], (
                        f"{role_logical_id}: unexpected wildcard-resource statement for actions {actions!r}"
                    )
                    assert statement.get("Condition") == {"StringEquals": {"cloudwatch:namespace": "SentinelGrid"}}
                else:
                    assert "*" not in resources

        logging_policy = _find_policy(props, f"{function_name}Logging")
        resources = logging_policy["PolicyDocument"]["Statement"][0]["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        assert len(resources) == 1
        resource_value = resources[0]
        # Fn::Sub string must reference exactly this function's own log group.
        assert isinstance(resource_value, dict) and "Fn::Sub" in resource_value
        assert f"/aws/lambda/{function_name}:*" in resource_value["Fn::Sub"]


# --- 11. No environment variables contain credentials ---


def test_no_environment_variables_with_credentials():
    """Step 33: Planner/Executor define RECOVERY_EVENTS_TABLE_NAME (a
    table name — not a secret). Step 34: all three now also define
    NODE_STATE_TABLE_NAME. Both populated via CloudFormation reference
    rather than a literal string. No environment variable anywhere may
    look like a credential.
    """
    template = _load_template()
    expected_env_vars = {
        "PlannerFunction": {"RECOVERY_EVENTS_TABLE_NAME", "NODE_STATE_TABLE_NAME"},
        "ExecutorFunction": {"RECOVERY_EVENTS_TABLE_NAME", "NODE_STATE_TABLE_NAME"},
        "VerifierFunction": {"NODE_STATE_TABLE_NAME"},
    }
    for logical_id in FUNCTION_NAMES:
        props = template["Resources"][logical_id]["Properties"]
        env_vars = props.get("Environment", {}).get("Variables", {})
        assert set(env_vars.keys()) == expected_env_vars[logical_id]
        for key, value in env_vars.items():
            raw_value = str(value).lower()
            for forbidden in ("akia", "secret", "password", "access_key"):
                assert forbidden not in raw_value, f"{logical_id}.{key} looks like a credential"


def test_recovery_events_table_name_env_var_uses_cloudformation_reference_not_a_literal():
    template = _load_template()
    for logical_id in ("PlannerFunction", "ExecutorFunction"):
        props = template["Resources"][logical_id]["Properties"]
        value = props["Environment"]["Variables"]["RECOVERY_EVENTS_TABLE_NAME"]
        assert value == {"Fn::ImportValue": "SentinelGridRecoveryEventsTableName"}


def test_node_state_table_name_env_var_uses_cloudformation_reference_not_a_literal():
    template = _load_template()
    for logical_id in ("PlannerFunction", "ExecutorFunction", "VerifierFunction"):
        props = template["Resources"][logical_id]["Properties"]
        value = props["Environment"]["Variables"]["NODE_STATE_TABLE_NAME"]
        assert value == {"Fn::ImportValue": "SentinelGridNodeStateTableName"}


# --- 12. S3 deployment parameters / version pinning present ---


def test_s3_deployment_parameters_and_version_pinning_present():
    template = _load_template()
    params = template["Parameters"]

    assert "DeploymentBucketName" in params
    assert "Default" not in params["DeploymentBucketName"]

    for prefix in ("Planner", "Executor", "Verifier"):
        key_param = f"{prefix}PackageKey"
        version_param = f"{prefix}PackageVersion"
        assert key_param in params
        assert "Default" in params[key_param]
        assert version_param in params
        assert "Default" not in params[version_param]  # required, no default

    for logical_id, prefix in zip(FUNCTION_NAMES, ("Planner", "Executor", "Verifier")):
        code = template["Resources"][logical_id]["Properties"]["Code"]
        assert code["S3Bucket"] == {"Ref": "DeploymentBucketName"}
        assert code["S3Key"] == {"Ref": f"{prefix}PackageKey"}
        assert code["S3ObjectVersion"] == {"Ref": f"{prefix}PackageVersion"}


# --- 13. Outputs expose the three Lambda ARNs ---


def test_outputs_expose_all_three_lambda_arns():
    template = _load_template()
    outputs = template["Outputs"]
    assert outputs["PlannerFunctionArn"]["Value"] == {"Fn::GetAtt": ["PlannerFunction", "Arn"]}
    assert outputs["ExecutorFunctionArn"]["Value"] == {"Fn::GetAtt": ["ExecutorFunction", "Arn"]}
    assert outputs["VerifierFunctionArn"]["Value"] == {"Fn::GetAtt": ["VerifierFunction", "Arn"]}
    assert outputs["PlannerFunctionArn"]["Export"]["Name"] == "SentinelGridRemediationPlannerArn"
    assert outputs["ExecutorFunctionArn"]["Export"]["Name"] == "SentinelGridRemediationExecutorArn"
    assert outputs["VerifierFunctionArn"]["Export"]["Name"] == "SentinelGridRemediationVerifierArn"


# --- 14. Template does not reference the anomaly detector role ---


def test_template_does_not_reference_anomaly_detector_role_or_resources():
    raw = TEMPLATE_PATH.read_text()
    for name in (
        "SentinelGridAnomalyDetectorExecutionRole",
        "SentinelGridAnomalyDetector",
        "AnomalyDetectorFunction",
        "AnomalyDetectorExecutionRole",
    ):
        assert name not in raw


# --- 15. Template does not modify/import unrelated infrastructure ---


def test_template_creates_only_expected_resource_types():
    template = _load_template()
    actual_types = {r["Type"] for r in template["Resources"].values()}
    assert actual_types == {"AWS::IAM::Role", "AWS::Lambda::Function"}


def test_template_only_imports_the_recovery_and_node_state_table_exports():
    """Step 33 introduces two deliberate cross-stack references (the
    recovery-events table's ARN and name); Step 34 adds two more (the
    node-state table's ARN and name). No other ImportValue usage is
    expected.
    """
    template = _load_template()
    raw = TEMPLATE_PATH.read_text()
    import_matches = re.findall(r"!ImportValue\s+(\S+)", raw)
    assert set(import_matches) == {
        "SentinelGridRecoveryEventsTableArn",
        "SentinelGridRecoveryEventsTableName",
        "SentinelGridNodeStateTableArn",
        "SentinelGridNodeStateTableName",
    }
    assert "Fn::ImportValue" not in raw  # only the short-form !ImportValue is used, consistently


def test_other_infrastructure_templates_still_parse_unchanged():
    for filename in (
        "telemetry-storage.yaml",
        "anomaly-detector.yaml",
        "anomaly-detection-rule.yaml",
        "anomaly-events-storage.yaml",
        "remediation-workflow.yaml",
    ):
        load_cfn_template(REPO_ROOT / "infrastructure" / filename)  # must not raise


def test_deploy_script_uses_existing_bucket_and_correct_stack_name():
    script = DEPLOY_SCRIPT.read_text()
    assert 'STACK_NAME="${STACK_NAME:-sentinelgrid-remediation-lambdas}"' in script
    assert 'AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"' in script
    assert 'AWS_REGION="${AWS_REGION:-ap-south-1}"' in script
    assert "CAPABILITY_NAMED_IAM" in script
    # Must not create a new bucket.
    assert "s3api create-bucket" not in script
    assert "s3 mb" not in script


# ============================================================================
# PACKAGING TESTS
# ============================================================================


def _build_packages(output_dir: Path):
    subprocess.run(
        [str(PACKAGE_SCRIPT), str(output_dir)],
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _zip_entries(zip_path: Path) -> set:
    import zipfile

    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


def test_package_script_exists():
    assert PACKAGE_SCRIPT.exists()


def test_all_three_zips_contain_required_modules_and_nothing_forbidden():
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        _build_packages(output_dir)

        for name in ("planner", "executor", "verifier"):
            zip_path = output_dir / f"remediation-{name}.zip"
            assert zip_path.exists(), f"{zip_path} was not built"
            entries = _zip_entries(zip_path)

            missing = REQUIRED_ZIP_ENTRIES - entries
            assert not missing, f"remediation-{name}.zip is missing required module(s): {missing}"

            for forbidden in FORBIDDEN_ZIP_SUBSTRINGS:
                offending = [e for e in entries if forbidden in e]
                assert not offending, f"remediation-{name}.zip unexpectedly contains: {offending}"


def test_no_credentials_or_secrets_in_packaged_source():
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        _build_packages(output_dir)

        secret_pattern = re.compile(r"AKIA[0-9A-Z]{16}|BEGIN[ A-Z]*PRIVATE KEY|aws_secret_access_key", re.IGNORECASE)
        for name in ("planner", "executor", "verifier"):
            zip_path = output_dir / f"remediation-{name}.zip"
            import zipfile

            with zipfile.ZipFile(zip_path) as zf:
                for entry in zf.namelist():
                    if entry.endswith(".py"):
                        content = zf.read(entry).decode("utf-8", errors="replace")
                        assert not secret_pattern.search(content), f"possible secret found in {entry}"


def test_each_zip_handler_imports_successfully_in_isolation():
    handler_module_by_name = {
        "planner": "lambda_functions.remediation.planner",
        "executor": "lambda_functions.remediation.executor",
        "verifier": "lambda_functions.remediation.verifier",
    }
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        _build_packages(output_dir)

        with tempfile.TemporaryDirectory() as isolated_cwd:
            for name, module in handler_module_by_name.items():
                zip_path = output_dir / f"remediation-{name}.zip"
                code = (
                    f"import sys; sys.path.insert(0, {str(zip_path)!r}); "
                    f"import importlib; m = importlib.import_module({module!r}); "
                    f"assert hasattr(m, 'lambda_handler')"
                )
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=isolated_cwd,
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0, (
                    f"isolated import of {module} from remediation-{name}.zip failed:\n"
                    f"stdout={result.stdout}\nstderr={result.stderr}"
                )


def test_packaging_is_reproducible_same_content_each_run():
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        _build_packages(Path(tmp1))
        _build_packages(Path(tmp2))

        for name in ("planner", "executor", "verifier"):
            entries1 = _zip_entries(Path(tmp1) / f"remediation-{name}.zip")
            entries2 = _zip_entries(Path(tmp2) / f"remediation-{name}.zip")
            assert entries1 == entries2
