"""Tests for the Step 48 operator-identity CloudFormation template and
audit script. These are static/structural tests that do NOT require live
AWS credentials -- they parse infrastructure/operator-identity.yaml and
scripts/step48_operator_identity_audit.py directly, so they run as part
of the normal `pytest` suite in any environment.

Live-AWS verification (does SentinelGridOperatorRole exist, can it be
assumed, are the 10 service roles unchanged) is performed by actually
running scripts/step48_operator_identity_audit.py against the real
account -- out of scope for unit tests, done manually as part of Step 48
(see docs/operator-identity.md).
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import _cfn_yaml  # noqa: E402

TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "operator-identity.yaml"


@pytest.fixture(scope="module")
def template():
    return _cfn_yaml.load_cfn_template(TEMPLATE_PATH)


@pytest.fixture(scope="module")
def role_properties(template):
    return template["Resources"]["SentinelGridOperatorRole"]["Properties"]


@pytest.fixture(scope="module")
def all_statements(role_properties):
    stmts = []
    for policy in role_properties["Policies"]:
        for stmt in policy["PolicyDocument"]["Statement"]:
            stmts.append({"policy_name": policy["PolicyName"], **stmt})
    return stmts


def test_template_creates_exactly_one_resource(template):
    assert list(template["Resources"].keys()) == ["SentinelGridOperatorRole"]


def test_role_is_named_sentinelgrid_operator_role(role_properties):
    assert role_properties["RoleName"] == "SentinelGridOperatorRole"


def test_description_fits_cloudformation_limit(template):
    assert len(template["Description"]) <= 1024


def test_trust_policy_does_not_grant_cross_account_access(role_properties):
    trust_stmt = role_properties["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust_stmt) == 1
    principal = trust_stmt[0]["Principal"]["AWS"]
    # Must reference this account's own root ARN via the parameter default,
    # not a literal wildcard or a different account.
    assert principal == {"Ref": "TrustedPrincipalArn"} or "401429382000" in str(principal)


def test_trust_policy_only_allows_assume_role_action(role_properties):
    trust_stmt = role_properties["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust_stmt["Action"] == "sts:AssumeRole"
    assert trust_stmt["Effect"] == "Allow"


def test_no_iam_wildcard_action_anywhere(all_statements):
    for stmt in all_statements:
        actions = stmt.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        assert "iam:*" not in actions
        assert "*" not in actions, f"Action:'*' found in {stmt['policy_name']}"


def test_no_forbidden_account_level_services_granted(all_statements):
    forbidden_prefixes = ("organizations:", "account:", "billing:", "aws-portal:")
    for stmt in all_statements:
        actions = stmt.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        for action in actions:
            assert not action.startswith(forbidden_prefixes), f"Forbidden action {action} in {stmt['policy_name']}"


def test_no_administrator_equivalent_statement(all_statements):
    for stmt in all_statements:
        actions = stmt.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        resources = stmt.get("Resource", [])
        resources = [resources] if isinstance(resources, str) else resources
        is_admin_like = actions == ["*"] and resources == ["*"]
        assert not is_admin_like


def test_pass_role_statements_never_use_wildcard_resource(all_statements):
    pass_role_stmts = [s for s in all_statements if "iam:PassRole" in (s.get("Action") if isinstance(s.get("Action"), list) else [s.get("Action")])]
    assert len(pass_role_stmts) == 3  # Lambda, States, IoT
    for stmt in pass_role_stmts:
        resources = stmt["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        assert "*" not in resources
        for r in resources:
            assert r.startswith("arn:aws:iam::401429382000:role/SentinelGrid")


def test_pass_role_statements_each_have_passed_to_service_condition(all_statements):
    pass_role_stmts = [s for s in all_statements if "iam:PassRole" in (s.get("Action") if isinstance(s.get("Action"), list) else [s.get("Action")])]
    for stmt in pass_role_stmts:
        assert "Condition" in stmt
        assert "iam:PassedToService" in stmt["Condition"]["StringEquals"]


def test_pass_role_covers_exactly_the_ten_known_service_roles(all_statements):
    pass_role_stmts = [s for s in all_statements if "iam:PassRole" in (s.get("Action") if isinstance(s.get("Action"), list) else [s.get("Action")])]
    all_resources = []
    for stmt in pass_role_stmts:
        resources = stmt["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        all_resources.extend(resources)
    expected = {
        "SentinelGridAnomalyDetectorExecutionRole",
        "SentinelGridSqsConsumerExecutionRole",
        "SentinelGridRemediationTriggerExecutionRole",
        "SentinelGridRemediationPlannerRole",
        "SentinelGridRemediationExecutorRole",
        "SentinelGridRemediationVerifierRole",
        "SentinelGridKnowledgeQueryExecutionRole",
        "SentinelGridRemediationWorkflowRole",
        "SentinelGridIoTToDynamoDBRole",
        "SentinelGridIoTToSqsRole",
    }
    actual = {r.split("/")[-1] for r in all_resources}
    assert actual == expected


def test_no_dynamodb_mutation_actions_granted(all_statements):
    """The operator must not be able to mutate application data directly
    -- PutItem/UpdateItem/DeleteItem stay with the app's own Lambda
    execution roles."""
    forbidden = {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"}
    for stmt in all_statements:
        actions = stmt.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        assert forbidden.isdisjoint(actions), f"Data-mutation action found in {stmt['policy_name']}"


def test_no_sns_publish_or_subscribe_granted(all_statements):
    forbidden = {"sns:Publish", "sns:Subscribe"}
    for stmt in all_statements:
        actions = stmt.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        assert forbidden.isdisjoint(actions)


def test_wildcard_resource_statements_are_all_on_the_known_unavoidable_list(all_statements):
    """Every Resource:'*' statement must be one of the small set of
    documented, AWS-API-unavoidable cases (no resource-level ARN support
    for that action) -- see docs/operator-identity.md."""
    known_unavoidable_actions = {
        "cloudformation:ValidateTemplate",
        "cloudformation:GetTemplateSummary",
        "cloudformation:ListStacks",
        "lambda:ListFunctions",
        "dynamodb:ListTables",
        "sqs:ListQueues",
        "iot:ListTopicRules",
        "states:ListStateMachines",
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics",
        "cloudwatch:DescribeAlarms",
        "cloudwatch:ListDashboards",
        "sns:ListTopics",
    }
    for stmt in all_statements:
        resources = stmt.get("Resource", [])
        resources = [resources] if isinstance(resources, str) else resources
        if "*" in resources:
            actions = stmt.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            assert set(actions).issubset(known_unavoidable_actions), (
                f"Unexpected Resource:'*' for actions {actions} in {stmt['policy_name']}"
            )


def test_iam_role_management_scoped_to_sentinelgrid_prefix_only(all_statements):
    iam_manage_stmts = [
        s
        for s in all_statements
        if any(
            a in (s.get("Action") if isinstance(s.get("Action"), list) else [s.get("Action")])
            for a in ("iam:CreateRole", "iam:DeleteRole", "iam:PutRolePolicy")
        )
    ]
    assert len(iam_manage_stmts) == 1
    resources = iam_manage_stmts[0]["Resource"]
    resources = [resources] if isinstance(resources, str) else resources
    for r in resources:
        assert r == "arn:aws:iam::401429382000:role/SentinelGrid*"


def test_s3_permissions_scoped_to_deploy_bucket_only_not_frontend_bucket(all_statements):
    for stmt in all_statements:
        actions = stmt.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        if any(a.startswith("s3:") for a in actions):
            resources = stmt["Resource"]
            resources = [resources] if isinstance(resources, str) else resources
            for r in resources:
                assert "sentinelgrid-lambda-deploy-401429382000" in r


def test_audit_script_importable_and_has_expected_role_map():
    from scripts.step48_operator_identity_audit import SERVICE_ROLE_POLICIES, OPERATOR_ROLE_ARN

    assert len(SERVICE_ROLE_POLICIES) == 10
    assert OPERATOR_ROLE_ARN == "arn:aws:iam::401429382000:role/SentinelGridOperatorRole"


def test_step48_results_json_is_well_formed_if_present():
    results_path = REPO_ROOT / "docs" / "step48_operator_identity_results.json"
    if not results_path.exists():
        return
    with open(results_path) as f:
        data = json.load(f)
    assert "identity" in data
    assert "operator_role" in data
    assert "assume_role_probe" in data
