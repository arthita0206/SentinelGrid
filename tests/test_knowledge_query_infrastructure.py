"""Structural tests for the knowledge-query infrastructure
(infrastructure/knowledge-query.yaml, its deploy/package scripts, and the
Step 35 addition to infrastructure/anomaly-events-storage.yaml).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no DynamoDB/Lambda/API Gateway
access, no credentials, and no deployment. Mirrors
tests/test_remediation_lambda_infrastructure.py's own conventions.
"""

import subprocess
import zipfile
from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "knowledge-query.yaml"
DEPLOY_SCRIPT = REPO_ROOT / "infrastructure" / "deploy-knowledge-query.sh"
PACKAGE_SCRIPT = REPO_ROOT / "infrastructure" / "package-knowledge-query.sh"
ANOMALY_EVENTS_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "anomaly-events-storage.yaml"
NODE_STATE_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "node-state-storage.yaml"
RECOVERY_EVENTS_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "recovery-events-storage.yaml"
REMEDIATION_WORKFLOW_TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "remediation-workflow.yaml"

REQUIRED_ZIP_ENTRIES = {
    "lambda_functions/__init__.py",
    "lambda_functions/knowledge_query/__init__.py",
    "lambda_functions/knowledge_query/handler.py",
    "lambda_functions/knowledge_query/query_service.py",
    "lambda_functions/knowledge_query/metrics_service.py",
}


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _find_resources_by_type(resources: dict, resource_type: str) -> list:
    return [r for r in resources.values() if r.get("Type") == resource_type]


def _find_resource_by_type(resources: dict, resource_type: str) -> dict:
    matches = _find_resources_by_type(resources, resource_type)
    assert len(matches) == 1, f"expected exactly one {resource_type}, found {len(matches)}"
    return matches[0]


# --- Template structure ---


def test_template_file_exists():
    assert TEMPLATE_PATH.exists()


def test_template_parses_and_has_expected_top_level_sections():
    template = _load_template()
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Resources" in template


def test_top_level_description_within_limit():
    template = _load_template()
    assert len(template["Description"]) <= 1024


# --- Lambda function ---


def test_lambda_function_exists_with_expected_configuration():
    resources = _load_template()["Resources"]
    fn = _find_resource_by_type(resources, "AWS::Lambda::Function")
    props = fn["Properties"]

    assert props["FunctionName"] == "SentinelGridKnowledgeQuery"
    assert props["Handler"] == "lambda_functions.knowledge_query.handler.lambda_handler"
    assert props["Runtime"].startswith("python3")
    assert len(props["Description"]) <= 256


def test_lambda_environment_variables_present_and_reference_imports():
    resources = _load_template()["Resources"]
    fn = _find_resource_by_type(resources, "AWS::Lambda::Function")
    env = fn["Properties"]["Environment"]["Variables"]

    assert env["NODE_STATE_TABLE_NAME"] == {"Fn::ImportValue": "SentinelGridNodeStateTableName"}
    assert env["ANOMALY_EVENTS_TABLE_NAME"] == {"Fn::ImportValue": "SentinelGridAnomalyEventsTableName"}
    assert env["RECOVERY_EVENTS_TABLE_NAME"] == {"Fn::ImportValue": "SentinelGridRecoveryEventsTableName"}


def test_no_hardcoded_table_name_literal_in_environment_variables():
    template = _load_template()
    fn = _find_resource_by_type(template["Resources"], "AWS::Lambda::Function")
    env = fn["Properties"]["Environment"]["Variables"]
    for value in env.values():
        assert isinstance(value, dict) and "Fn::ImportValue" in value


def test_lambda_code_pinned_to_s3_object_version():
    resources = _load_template()["Resources"]
    fn = _find_resource_by_type(resources, "AWS::Lambda::Function")
    code = fn["Properties"]["Code"]
    assert code["S3ObjectVersion"] == {"Ref": "DeploymentPackageVersion"}


# --- IAM least privilege ---


def test_execution_role_grants_only_expected_dynamodb_actions():
    # Step 40: dynamodb:Scan was added (GET /nodes, NodeState table only)
    # alongside the existing GetItem/Query -- still no write action.
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    dynamodb_actions = set()
    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            dynamodb_actions.update(a for a in actions if a.startswith("dynamodb:"))

    assert dynamodb_actions == {"dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"}


def test_no_dynamodb_write_actions_anywhere():
    # Scan is intentionally granted (Step 40, NodeState only -- see
    # test_scan_action_is_scoped_to_the_node_state_table_only below for
    # the ARN-scoping check) -- this test only forbids WRITE actions.
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    forbidden = {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:*"}
    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            assert not (actions & forbidden), f"forbidden action found: {actions & forbidden}"


def test_scan_action_is_scoped_to_exactly_the_three_known_tables():
    # Step 42: Scan is now also granted on AnomalyEvents/RecoveryEvents
    # (GET /events), alongside the existing NodeState grant (GET /nodes)
    # -- every dynamodb:Scan statement must reference exactly one of the
    # three known table ARNs, never anything else, never a wildcard.
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    known_table_arns = {
        "SentinelGridNodeStateTableArn",
        "SentinelGridAnomalyEventsTableArn",
        "SentinelGridRecoveryEventsTableArn",
    }
    scan_resource_arns = set()

    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            if "dynamodb:Scan" in actions:
                resource = statement["Resource"]
                resource = [resource] if isinstance(resource, str) else resource
                assert len(resource) == 1
                assert isinstance(resource[0], dict) and "Fn::ImportValue" in resource[0]
                scan_resource_arns.add(resource[0]["Fn::ImportValue"])

    assert scan_resource_arns == known_table_arns


def test_node_state_scan_is_granted_alongside_get_item_in_the_same_statement():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            resource = statement["Resource"]
            resource = [resource] if isinstance(resource, str) else resource
            if resource == [{"Fn::ImportValue": "SentinelGridNodeStateTableArn"}]:
                assert actions == {"dynamodb:GetItem", "dynamodb:Scan"}


def test_anomaly_and_recovery_events_scan_is_granted_alongside_query_in_the_same_statement():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            resource = statement["Resource"]
            resource = [resource] if isinstance(resource, str) else resource
            if resource in (
                [{"Fn::ImportValue": "SentinelGridAnomalyEventsTableArn"}],
                [{"Fn::ImportValue": "SentinelGridRecoveryEventsTableArn"}],
            ):
                assert actions == {"dynamodb:Query", "dynamodb:Scan"}


def test_no_wildcard_dynamodb_resource():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            if any(a.startswith("dynamodb:") for a in actions):
                resource = statement["Resource"]
                resource = [resource] if isinstance(resource, str) else resource
                assert "*" not in resource


def test_dynamodb_policies_reference_exact_imported_arns():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    imported_arns = set()
    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            if any(a.startswith("dynamodb:") for a in actions):
                resource = statement["Resource"]
                resource = [resource] if isinstance(resource, str) else resource
                for r in resource:
                    assert isinstance(r, dict) and "Fn::ImportValue" in r
                    imported_arns.add(r["Fn::ImportValue"])

    assert imported_arns == {
        "SentinelGridNodeStateTableArn",
        "SentinelGridAnomalyEventsTableArn",
        "SentinelGridRecoveryEventsTableArn",
    }


# --- IAM least privilege (CloudWatch metrics, Step 43) ---


def test_execution_role_grants_exactly_get_metric_data_for_cloudwatch():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    cloudwatch_actions = set()
    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            cloudwatch_actions.update(a for a in actions if a.startswith("cloudwatch:"))

    assert cloudwatch_actions == {"cloudwatch:GetMetricData"}


def test_no_cloudwatch_write_or_alarm_actions_anywhere():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    forbidden = {
        "cloudwatch:PutMetricData",
        "cloudwatch:PutMetricAlarm",
        "cloudwatch:SetAlarmState",
        "cloudwatch:DeleteAlarms",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:*",
    }
    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            assert not (actions & forbidden), f"forbidden CloudWatch action found: {actions & forbidden}"


def test_get_metric_data_resource_is_wildcard_with_no_condition():
    # cloudwatch:GetMetricData has no ARN-level resource scoping at all
    # (unlike DynamoDB) -- Resource: "*" is the only valid value for this
    # action. Deliberately NO cloudwatch:namespace Condition (unlike this
    # project's own PutMetricData grants, Step 28): live evidence
    # (aws iam simulate-principal-policy against the deployed role, and
    # the deployed Lambda's own CloudWatch Logs) confirmed that condition
    # key is never populated for GetMetricData -- its namespace is
    # expressed inside MetricDataQueries[].MetricStat.Metric.Namespace,
    # not a top-level request parameter AWS surfaces as a context key --
    # so a Condition here would silently deny every legitimate request,
    # which is exactly what happened on first deploy. The application
    # code (metrics_service.py's fixed NAMESPACE constant, never
    # caller-controlled) is the sole enforcement of "SentinelGrid
    # namespace only" for this specific action.
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    found = False
    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            if "cloudwatch:GetMetricData" in actions:
                found = True
                assert statement["Resource"] == "*"
                assert "Condition" not in statement
    assert found, "expected a cloudwatch:GetMetricData statement"


def test_get_metric_data_is_granted_in_its_own_dedicated_statement():
    # Not bundled into any DynamoDB statement -- CloudWatch and DynamoDB
    # actions are never mixed in one IAM statement in this template.
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            if "cloudwatch:GetMetricData" in actions:
                assert not any(a.startswith("dynamodb:") for a in actions)


def test_execution_role_has_logging_policy():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")

    logging_actions = set()
    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = {actions} if isinstance(actions, str) else set(actions)
            logging_actions.update(a for a in actions if a.startswith("logs:"))

    assert {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"} <= logging_actions


def test_role_is_dedicated_not_reused():
    resources = _load_template()["Resources"]
    role = _find_resource_by_type(resources, "AWS::IAM::Role")
    assert role["Properties"]["RoleName"] == "SentinelGridKnowledgeQueryExecutionRole"


# --- API Gateway HTTP API ---


def test_http_api_exists_with_http_protocol():
    resources = _load_template()["Resources"]
    api = _find_resource_by_type(resources, "AWS::ApiGatewayV2::Api")
    assert api["Properties"]["ProtocolType"] == "HTTP"


def test_integration_exists_and_targets_the_lambda():
    resources = _load_template()["Resources"]
    integration = _find_resource_by_type(resources, "AWS::ApiGatewayV2::Integration")
    props = integration["Properties"]
    assert props["IntegrationType"] == "AWS_PROXY"
    assert props["PayloadFormatVersion"] == "2.0"
    assert props["IntegrationUri"] == {"Fn::GetAtt": ["KnowledgeQueryFunction", "Arn"]}


def test_exactly_seven_routes_all_get():
    resources = _load_template()["Resources"]
    routes = _find_resources_by_type(resources, "AWS::ApiGatewayV2::Route")
    assert len(routes) == 7
    route_keys = {r["Properties"]["RouteKey"] for r in routes}
    assert route_keys == {
        "GET /nodes",
        "GET /events",
        "GET /metrics",
        "GET /nodes/{node_id}",
        "GET /nodes/{node_id}/anomalies",
        "GET /nodes/{node_id}/recoveries",
        "GET /nodes/{node_id}/summary",
    }


def test_no_write_http_methods_in_any_route():
    resources = _load_template()["Resources"]
    routes = _find_resources_by_type(resources, "AWS::ApiGatewayV2::Route")
    for route in routes:
        route_key = route["Properties"]["RouteKey"]
        for forbidden_method in ("POST", "PUT", "PATCH", "DELETE"):
            assert not route_key.startswith(f"{forbidden_method} "), f"unexpected write route: {route_key}"


def test_all_routes_target_the_single_integration():
    resources = _load_template()["Resources"]
    routes = _find_resources_by_type(resources, "AWS::ApiGatewayV2::Route")
    for route in routes:
        assert route["Properties"]["Target"] == {"Fn::Sub": "integrations/${KnowledgeQueryIntegration}"}


def test_stage_exists():
    resources = _load_template()["Resources"]
    stage = _find_resource_by_type(resources, "AWS::ApiGatewayV2::Stage")
    assert stage["Properties"]["StageName"] == "$default"


# --- CORS (Step 40) ---


def test_cors_configuration_exists_on_the_http_api():
    resources = _load_template()["Resources"]
    api = _find_resource_by_type(resources, "AWS::ApiGatewayV2::Api")
    assert "CorsConfiguration" in api["Properties"]


def test_cors_allows_only_get():
    resources = _load_template()["Resources"]
    api = _find_resource_by_type(resources, "AWS::ApiGatewayV2::Api")
    cors = api["Properties"]["CorsConfiguration"]
    assert cors["AllowMethods"] == ["GET"]


def test_cors_origin_is_not_a_wildcard():
    resources = _load_template()["Resources"]
    api = _find_resource_by_type(resources, "AWS::ApiGatewayV2::Api")
    cors = api["Properties"]["CorsConfiguration"]
    assert "*" not in cors["AllowOrigins"]


def test_cors_origin_references_the_frontend_origin_parameter():
    resources = _load_template()["Resources"]
    api = _find_resource_by_type(resources, "AWS::ApiGatewayV2::Api")
    cors = api["Properties"]["CorsConfiguration"]
    assert cors["AllowOrigins"] == [{"Ref": "FrontendOrigin"}]


def test_frontend_origin_parameter_defaults_to_local_vite_dev_server():
    template = _load_template()
    param = template["Parameters"]["FrontendOrigin"]
    assert param["Default"] == "http://localhost:5173"


def test_cors_does_not_allow_write_methods():
    resources = _load_template()["Resources"]
    api = _find_resource_by_type(resources, "AWS::ApiGatewayV2::Api")
    cors = api["Properties"]["CorsConfiguration"]
    for forbidden_method in ("POST", "PUT", "PATCH", "DELETE"):
        assert forbidden_method not in cors["AllowMethods"]


def test_exactly_three_lambda_permissions_one_per_top_level_resource_path():
    # Step 43: a third AWS::Lambda::Permission was added for the new
    # "/metrics" resource path, alongside the existing "/nodes*"/"/events*"
    # ones -- deliberately not folded into one broader grant.
    resources = _load_template()["Resources"]
    permissions = _find_resources_by_type(resources, "AWS::Lambda::Permission")
    assert len(permissions) == 3

    source_arn_subs = {p["Properties"]["SourceArn"]["Fn::Sub"] for p in permissions}
    assert any(s.endswith("/nodes*") for s in source_arn_subs)
    assert any(s.endswith("/events*") for s in source_arn_subs)
    assert any(s.endswith("/metrics*") for s in source_arn_subs)


def test_lambda_permissions_are_scoped_to_this_api_not_wildcard():
    resources = _load_template()["Resources"]
    permissions = _find_resources_by_type(resources, "AWS::Lambda::Permission")

    for permission in permissions:
        props = permission["Properties"]
        assert props["Action"] == "lambda:InvokeFunction"
        assert props["Principal"] == "apigateway.amazonaws.com"
        source_arn_sub = props["SourceArn"]["Fn::Sub"]
        assert "${KnowledgeQueryApi}" in source_arn_sub
        assert source_arn_sub != "*"


def test_lambda_permissions_are_not_public_invoke_grants():
    # A public grant would use Principal "*" or omit SourceArn entirely.
    resources = _load_template()["Resources"]
    permissions = _find_resources_by_type(resources, "AWS::Lambda::Permission")

    for permission in permissions:
        props = permission["Properties"]
        assert props["Principal"] != "*"
        assert "SourceArn" in props


# --- No unnecessary resources ---


def test_no_ec2_ecs_eks_rds_appsync_or_waf_resources():
    resources = _load_template()["Resources"]
    forbidden_types = (
        "AWS::EC2::Instance",
        "AWS::ECS::Cluster",
        "AWS::EKS::Cluster",
        "AWS::RDS::DBInstance",
        "AWS::AppSync::GraphQLApi",
        "AWS::WAFv2::WebACL",
        "AWS::ApiGateway::RestApi",  # explicitly REST API, not HTTP API
        "AWS::Route53::RecordSet",
        "AWS::ApiGatewayV2::DomainName",
    )
    for resource in resources.values():
        assert resource["Type"] not in forbidden_types


def test_no_dynamodb_table_created_here_only_referenced():
    resources = _load_template()["Resources"]
    assert not _find_resources_by_type(resources, "AWS::DynamoDB::Table")


def test_no_hardcoded_credentials_or_account_ids_in_template():
    raw_text = TEMPLATE_PATH.read_text().lower()
    for forbidden in ("akia", "secret", "access_key", "accesskey", "password", "arn:aws:iam::123456789012"):
        assert forbidden not in raw_text


def test_outputs_include_lambda_arn_api_id_endpoint_and_stage():
    template = _load_template()
    outputs = template["Outputs"]
    assert "FunctionArn" in outputs
    assert "HttpApiId" in outputs
    assert "HttpApiEndpoint" in outputs
    assert "StageName" in outputs


# --- Deployment script ---


def test_deploy_script_exists_and_is_executable():
    assert DEPLOY_SCRIPT.exists()
    assert DEPLOY_SCRIPT.stat().st_mode & 0o111


def test_deploy_script_uses_expected_stack_name():
    text = DEPLOY_SCRIPT.read_text()
    assert "sentinelgrid-knowledge-query" in text


def test_deploy_script_uses_sentinelgrid_profile_and_ap_south_1_by_default():
    text = DEPLOY_SCRIPT.read_text()
    assert "AWS_PROFILE:-sentinelgrid" in text
    assert "AWS_REGION:-ap-south-1" in text


def test_deploy_script_references_the_correct_template():
    text = DEPLOY_SCRIPT.read_text()
    assert "knowledge-query.yaml" in text


def test_deploy_script_contains_no_hardcoded_credentials():
    raw_text = DEPLOY_SCRIPT.read_text().lower()
    for forbidden in ("akia", "aws_secret_access_key=", "aws_access_key_id="):
        assert forbidden not in raw_text


def test_deploy_script_does_not_modify_other_stacks():
    text = DEPLOY_SCRIPT.read_text()
    assert "sentinelgrid-telemetry-storage" not in text
    assert "sentinelgrid-remediation-lambdas" not in text
    assert "sentinelgrid-anomaly-detector" not in text
    assert "update-function-code" not in text
    for forbidden in ("PutItem", "UpdateItem", "DeleteItem", "publish", "start-execution", "iot-data"):
        assert forbidden not in text


# --- Packaging script / package contents ---


def test_package_script_exists_and_is_executable():
    assert PACKAGE_SCRIPT.exists()
    assert PACKAGE_SCRIPT.stat().st_mode & 0o111


def test_package_script_produces_expected_contents(tmp_path):
    output_zip = tmp_path / "knowledge-query.zip"
    subprocess.run([str(PACKAGE_SCRIPT), str(output_zip)], check=True, cwd=str(REPO_ROOT))

    assert output_zip.exists()
    with zipfile.ZipFile(output_zip) as zf:
        names = {n for n in zf.namelist() if not n.endswith("/")}

    assert REQUIRED_ZIP_ENTRIES <= names


def test_package_does_not_include_unrelated_project_modules(tmp_path):
    output_zip = tmp_path / "knowledge-query.zip"
    subprocess.run([str(PACKAGE_SCRIPT), str(output_zip)], check=True, cwd=str(REPO_ROOT))

    with zipfile.ZipFile(output_zip) as zf:
        names = {n for n in zf.namelist() if not n.endswith("/")}

    for unexpected_prefix in ("lambda_functions/anomaly_detector/", "lambda_functions/remediation/", "remediation/", "shared/"):
        assert not any(n.startswith(unexpected_prefix) for n in names), f"unexpected module bundled: {unexpected_prefix}"


def test_package_handler_imports_successfully_in_isolation(tmp_path):
    output_zip = tmp_path / "knowledge-query.zip"
    subprocess.run([str(PACKAGE_SCRIPT), str(output_zip)], check=True, cwd=str(REPO_ROOT))

    script = (
        "import sys; "
        f"sys.path.insert(0, {str(output_zip)!r}); "
        "import lambda_functions.knowledge_query.handler as h; "
        "assert callable(h.lambda_handler); "
        "import lambda_functions.knowledge_query.query_service as qs; "
        "assert hasattr(qs, 'KnowledgeQueryService')"
    )
    result = subprocess.run(["python3", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- Anomaly-events-storage.yaml (Step 35 addition: table-name export) ---


def test_anomaly_events_table_name_is_now_exported():
    template = load_cfn_template(ANOMALY_EVENTS_TEMPLATE_PATH)
    export = template["Outputs"]["AnomalyEventsTableName"]["Export"]
    assert export["Name"] == "SentinelGridAnomalyEventsTableName"


def test_anomaly_events_storage_description_still_within_limit():
    template = load_cfn_template(ANOMALY_EVENTS_TEMPLATE_PATH)
    assert len(template["Description"]) <= 1024


def test_anomaly_events_storage_table_resource_itself_is_unchanged():
    # The Step 35 edit must be purely additive (a new Output/Export) --
    # the DynamoDB table's own Properties must be byte-for-byte identical
    # to before, per "preserve all existing behavior unless explicitly
    # required".
    resources = load_cfn_template(ANOMALY_EVENTS_TEMPLATE_PATH)["Resources"]
    table = resources["AnomalyEventsTable"]
    props = table["Properties"]
    assert props["TableName"] == "SentinelGridAnomalyEvents"
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    attr_types = {a["AttributeName"]: a["AttributeType"] for a in props["AttributeDefinitions"]}
    assert attr_types == {"node_id": "S", "timestamp": "S"}
    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"


# --- Isolation from existing infrastructure ---


def test_existing_templates_are_not_duplicated_or_redefined():
    new_resources = _load_template()["Resources"]
    assert "NodeStateTable" not in new_resources
    assert "AnomalyEventsTable" not in new_resources
    assert "RecoveryEventsTable" not in new_resources
    assert "AnomalyDetectorFunction" not in new_resources
    assert "PlannerFunction" not in new_resources
    assert "ExecutorFunction" not in new_resources
    assert "VerifierFunction" not in new_resources

    node_state_resources = load_cfn_template(NODE_STATE_TEMPLATE_PATH)["Resources"]
    assert node_state_resources["NodeStateTable"]["Properties"]["TableName"] == "SentinelGridNodeState"

    recovery_events_resources = load_cfn_template(RECOVERY_EVENTS_TEMPLATE_PATH)["Resources"]
    assert recovery_events_resources["RecoveryEventsTable"]["Properties"]["TableName"] == "SentinelGridRecoveryEvents"


def test_does_not_reference_step_functions_state_machine():
    text = TEMPLATE_PATH.read_text()
    assert "StateMachine" not in text
    assert "stepfunctions" not in text.lower()


def test_step_functions_workflow_template_is_untouched_by_this_template():
    # Sanity check only -- this template must not define or import
    # anything from the remediation workflow's own template.
    workflow_resources = load_cfn_template(REMEDIATION_WORKFLOW_TEMPLATE_PATH)["Resources"]
    assert len(workflow_resources) >= 1
