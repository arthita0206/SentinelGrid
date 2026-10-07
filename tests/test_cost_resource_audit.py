"""Tests for the Step 49 cost & resource audit script
(scripts/step49_cost_resource_audit.py). These are static/structural
tests that do NOT require live AWS credentials -- they check the
script's own source for mutation calls and validate the shape of any
previously-captured evidence file. Live-AWS verification (actual stack
inventory, resource configuration, Cost Explorer data) is performed by
actually running the script against the real account, done manually as
part of Step 49 (see docs/cost-resource-audit.md).
"""

import ast
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "step49_cost_resource_audit.py"

# boto3 client method name prefixes that mutate AWS state. Any call
# matching `<client>.<name>(` where name starts with one of these is a
# mutation and must never appear in this audit script.
MUTATING_PREFIXES = (
    "put_",
    "update_",
    "delete_",
    "create_",
    "start_",
    "stop_",
    "publish",
    "invoke",
    "add_permission",
    "remove_permission",
    "subscribe",
    "unsubscribe",
    "set_",
    "send_message",
    "receive_message",
    "purge_queue",
    "tag_",
    "untag_",
    "modify_",
    "register_",
    "deregister_",
)

READ_ONLY_EXCEPTIONS = {
    # get_* / list_* / describe_* are always fine; explicitly not mutating
    # despite starting with letters that could otherwise look ambiguous.
}


def _source():
    return SCRIPT_PATH.read_text()


def test_script_file_exists():
    assert SCRIPT_PATH.exists()


def test_script_has_no_mutating_boto3_calls():
    source = _source()
    # Find every `<something>.method_name(` call pattern.
    calls = re.findall(r"\.([a-z][a-z0-9_]*)\(", source)
    offending = [c for c in calls if any(c.startswith(p) for p in MUTATING_PREFIXES) and c not in READ_ONLY_EXCEPTIONS]
    assert offending == [], f"Mutating-looking boto3 calls found: {offending}"


def test_script_never_calls_lambda_invoke():
    assert "invoke(" not in _source()
    assert ".invoke_async(" not in _source()


def test_script_never_starts_step_functions_execution():
    assert "start_execution" not in _source()


def test_script_never_sends_sqs_or_publishes():
    source = _source()
    assert "send_message" not in source
    assert "sns.publish" not in source
    assert ".publish(" not in source


def test_script_is_valid_python():
    ast.parse(_source())


def test_script_defines_expected_audit_functions():
    tree = ast.parse(_source())
    fn_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    expected = {
        "audit_identity",
        "audit_lambda_quota",
        "audit_cloudformation_stacks",
        "audit_lambda",
        "audit_dynamodb",
        "audit_sqs",
        "audit_iot",
        "audit_step_functions",
        "audit_api_gateway",
        "audit_cloudwatch",
        "audit_sns",
        "audit_s3",
        "audit_iam",
        "audit_cost_explorer",
    }
    assert expected.issubset(fn_names)


def test_known_stack_names_include_kinesis_and_frontend():
    from scripts.step49_cost_resource_audit import KNOWN_STACK_NAMES

    assert "sentinelgrid-telemetry-kinesis-stream" in KNOWN_STACK_NAMES
    assert "sentinelgrid-frontend-hosting" in KNOWN_STACK_NAMES
    assert "sentinelgrid-operator-identity" in KNOWN_STACK_NAMES
    assert len(KNOWN_STACK_NAMES) == 17


def test_dynamodb_tables_list_has_four_known_tables():
    from scripts.step49_cost_resource_audit import DYNAMODB_TABLES

    assert set(DYNAMODB_TABLES) == {
        "SentinelGridTelemetry",
        "SentinelGridAnomalyEvents",
        "SentinelGridRecoveryEvents",
        "SentinelGridNodeState",
    }


def test_step49_results_json_is_well_formed_if_present():
    results_path = REPO_ROOT / "docs" / "step49_cost_resource_audit_results.json"
    if not results_path.exists():
        return
    with open(results_path) as f:
        data = json.load(f)
    expected_sections = {
        "identity",
        "lambda_account_quota",
        "cloudformation_stacks",
        "lambda",
        "dynamodb",
        "sqs",
        "iot",
        "step_functions",
        "api_gateway",
        "cloudwatch",
        "sns",
        "s3_deployment_bucket",
        "iam_roles_summary",
        "cost_explorer",
    }
    assert expected_sections.issubset(data.keys())


def test_step49_results_dynamodb_all_pay_per_request_if_present():
    """If live evidence exists, every table must be PAY_PER_REQUEST --
    this project never intentionally provisions fixed DynamoDB capacity,
    and a change here would be a real cost-risk finding worth flagging."""
    results_path = REPO_ROOT / "docs" / "step49_cost_resource_audit_results.json"
    if not results_path.exists():
        return
    with open(results_path) as f:
        data = json.load(f)
    for table, cfg in data["dynamodb"].items():
        if "error" in cfg:
            continue
        assert cfg["BillingMode"] == "PAY_PER_REQUEST", f"{table} is not PAY_PER_REQUEST"


def test_step49_results_no_lambda_reserved_concurrency_if_present():
    """No SentinelGrid Lambda should have reserved concurrency configured
    -- none was ever intentionally set in any deploy script/template, so
    a nonzero value here would indicate an unexpected, undocumented
    change worth investigating."""
    results_path = REPO_ROOT / "docs" / "step49_cost_resource_audit_results.json"
    if not results_path.exists():
        return
    with open(results_path) as f:
        data = json.load(f)
    for fn, cfg in data["lambda"].items():
        if "error" in cfg:
            continue
        assert cfg.get("ReservedConcurrentExecutions") is None, f"{fn} unexpectedly has reserved concurrency"
