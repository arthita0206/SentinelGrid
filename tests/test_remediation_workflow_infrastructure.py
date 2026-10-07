"""Structural tests for the Step Functions remediation-workflow
infrastructure (infrastructure/remediation-workflow.yaml) and its
deployment script (infrastructure/deploy-remediation-workflow.sh).

These tests do NOT call AWS in any way — no `aws cloudformation
validate-template`, no boto3 client, no network access, no credentials.
They validate local YAML/JSON structure only. See this project's actual
deployment report for the real AWS-side validation result.
"""

import json
import re
from pathlib import Path

from tests._cfn_yaml import load_cfn_template

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infrastructure" / "remediation-workflow.yaml"
DEPLOY_SCRIPT_PATH = REPO_ROOT / "infrastructure" / "deploy-remediation-workflow.sh"
DEFINITION_PATH = REPO_ROOT / "step_functions" / "remediation_workflow.json"

EXISTING_INFRA_TEMPLATES = [
    "telemetry-storage.yaml",
    "anomaly-detector.yaml",
    "anomaly-detection-rule.yaml",
    "anomaly-events-storage.yaml",
]

FORBIDDEN_RESOURCE_TYPE_SUBSTRINGS = (
    "AWS::EC2::",
    "AWS::ECS::",
    "AWS::SNS::",
    "AWS::SQS::",
    "AWS::Events::",  # EventBridge
    "AWS::DynamoDB::",
    "AWS::IoT::",
    "AWS::S3::",
    "AWS::Lambda::",  # no fake/placeholder Lambda functions
)


def _load_template() -> dict:
    return load_cfn_template(TEMPLATE_PATH)


def _resources_of_type(template: dict, resource_type: str) -> list:
    return [r for r in template["Resources"].values() if r["Type"] == resource_type]


# --- 1. Template parses successfully ---


def test_template_file_exists_and_parses():
    assert TEMPLATE_PATH.exists()
    template = _load_template()
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Resources" in template


# --- 2. Exactly one AWS::StepFunctions::StateMachine ---


def test_exactly_one_state_machine_resource():
    template = _load_template()
    matches = _resources_of_type(template, "AWS::StepFunctions::StateMachine")
    assert len(matches) == 1


# --- 3. Exactly one AWS::IAM::Role for the Step Functions execution role ---


def test_exactly_one_iam_role_resource():
    template = _load_template()
    matches = _resources_of_type(template, "AWS::IAM::Role")
    assert len(matches) == 1


# --- 4. The state machine is STANDARD ---


def test_state_machine_type_is_standard():
    template = _load_template()
    state_machine = _resources_of_type(template, "AWS::StepFunctions::StateMachine")[0]
    assert state_machine["Properties"]["StateMachineType"] == "STANDARD"


# --- 5. The state machine definition corresponds to remediation_workflow.json ---


def test_state_machine_references_the_local_definition_file_not_a_copy():
    template = _load_template()
    state_machine = _resources_of_type(template, "AWS::StepFunctions::StateMachine")[0]
    props = state_machine["Properties"]

    # Must reference the definition via DefinitionS3Location (the upload
    # of the exact local file, per the deploy script) — not embed a
    # second, potentially-divergent copy of the ASL via DefinitionString.
    assert "DefinitionS3Location" in props
    assert "DefinitionString" not in props
    assert "Definition" not in props

    location = props["DefinitionS3Location"]
    assert location["Bucket"] == {"Ref": "DefinitionBucketName"}
    assert location["Key"] == {"Ref": "DefinitionKey"}
    assert location["Version"] == {"Ref": "DefinitionVersion"}


def test_definition_key_default_matches_deploy_script_default():
    template = _load_template()
    default_key = template["Parameters"]["DefinitionKey"]["Default"]
    script_contents = DEPLOY_SCRIPT_PATH.read_text()
    assert f'DEFINITION_KEY:-{default_key}' in script_contents


def test_definition_version_parameter_has_no_default():
    # Must be supplied explicitly (from a real S3 upload) at deploy time —
    # never silently defaulted/invented.
    template = _load_template()
    params = template["Parameters"]
    assert "DefinitionVersion" in params
    assert "Default" not in params["DefinitionVersion"]


def test_definition_substitutions_map_arns_to_the_correct_non_reversed_states():
    # Step 18: verifies the mapping planner->PlanRemediation,
    # executor->ExecuteRemediation, verifier->VerifyRemediation is not
    # accidentally reversed or crossed, by cross-referencing against the
    # actual placeholder token used in each Task's Resource field in the
    # canonical ASL file (not a value duplicated/assumed here).
    template = _load_template()
    state_machine = _resources_of_type(template, "AWS::StepFunctions::StateMachine")[0]
    substitutions = state_machine["Properties"]["DefinitionSubstitutions"]

    definition = json.loads(DEFINITION_PATH.read_text())
    states = definition["States"]

    token_to_state = {
        states["PlanRemediation"]["Resource"].strip("${}"): "PlannerArn",
        states["ExecuteRemediation"]["Resource"].strip("${}"): "ExecutorArn",
        states["VerifyRemediation"]["Resource"].strip("${}"): "VerifierArn",
    }

    for token, expected_arn_suffix in token_to_state.items():
        assert token in substitutions, f"substitution token {token!r} (used by the ASL) is not defined in the template"
        import_name = substitutions[token]["Fn::ImportValue"]
        assert import_name == f"SentinelGridRemediation{expected_arn_suffix}", (
            f"token {token!r} maps to {import_name!r}, expected SentinelGridRemediation{expected_arn_suffix}"
        )


def test_definition_substitutions_cover_exactly_the_placeholders_used_and_nothing_extra():
    template = _load_template()
    state_machine = _resources_of_type(template, "AWS::StepFunctions::StateMachine")[0]
    substitutions = state_machine["Properties"]["DefinitionSubstitutions"]

    raw = DEFINITION_PATH.read_text()
    tokens_used_in_asl = set(re.findall(r"\$\{([A-Za-z0-9_]+)\}", raw))

    assert set(substitutions.keys()) == tokens_used_in_asl


def test_referenced_json_file_is_valid_and_matches_step14_workflow():
    assert DEFINITION_PATH.exists()
    with open(DEFINITION_PATH) as f:
        definition = json.load(f)  # must not raise
    assert definition["StartAt"] == "CheckAnomaly"
    expected_states = {
        "CheckAnomaly",
        "NoAnomaly",
        "PrepareRemediationPlanning",
        "PlanRemediation",
        "CheckAutomaticRemediation",
        "ExecuteRemediation",
        "VerifyRemediation",
        "CheckVerification",
        "Success",
        "Escalate",
        # Step 51 (FR-5.4): configurable retry/escalation states added to
        # the previously Step-14-only set above -- see
        # docs/step51_remediation_retry.md for the full design. Listed
        # separately here only for readability; all are required members
        # of the same set.
        "ValidateMaxRetries",
        "UseProvidedMaxRetries",
        "UseDefaultMaxRetries",
        "CheckExecutionSuccess",
        "CheckRetryLimit",
        "IncrementRetryCount",
    }
    assert set(definition["States"].keys()) == expected_states


def test_inputpath_resultpath_contract_unchanged_by_this_step():
    definition = json.loads(DEFINITION_PATH.read_text())
    states = definition["States"]

    assert states["PlanRemediation"]["InputPath"] == "$.detection_result"
    assert states["PlanRemediation"]["ResultPath"] == "$.remediation_plan"
    assert states["ExecuteRemediation"]["InputPath"] == "$"
    assert states["ExecuteRemediation"]["ResultPath"] == "$.remediation_result"
    assert states["VerifyRemediation"]["InputPath"] == "$"
    assert states["VerifyRemediation"]["ResultPath"] == "$.verification"


def _effective_resolved_definition() -> dict:
    """Constructs the exact definition AWS would receive after
    DefinitionSubstitutions resolves every ${Token} in the canonical ASL
    text — using the SAME substitution map declared in the template
    (read from the parsed template, not hand-duplicated), against
    representative resolved ARN values. Used to prove no placeholder
    string can survive substitution and no Task Resource is left
    unresolved.
    """
    template = _load_template()
    state_machine = _resources_of_type(template, "AWS::StepFunctions::StateMachine")[0]
    substitution_tokens = set(state_machine["Properties"]["DefinitionSubstitutions"].keys())

    fake_resolved_values = {token: f"arn:aws:lambda:ap-south-1:401429382000:function:{token}" for token in substitution_tokens}

    raw = DEFINITION_PATH.read_text()

    def repl(m):
        token = m.group(1)
        assert token in fake_resolved_values, f"ASL uses undeclared substitution token {token!r}"
        return fake_resolved_values[token]

    resolved_raw = re.sub(r"\$\{([A-Za-z0-9_]+)\}", repl, raw)
    return json.loads(resolved_raw)


def test_no_placeholder_resource_strings_survive_substitution():
    resolved = _effective_resolved_definition()
    resolved_raw = json.dumps(resolved)
    assert "${" not in resolved_raw

    for name in ("PlanRemediation", "ExecuteRemediation", "VerifyRemediation"):
        resource = resolved["States"][name]["Resource"]
        assert resource.startswith("arn:aws:lambda:"), f"{name}.Resource did not resolve to a Lambda ARN: {resource!r}"


# --- 6. No existing infrastructure stack/template is referenced for mutation ---


def test_template_does_not_reference_any_existing_stack_or_resource_name():
    raw = TEMPLATE_PATH.read_text()
    existing_names = [
        "SentinelGridAnomalyDetector",
        "SentinelGridAnomalyEvents",
        "SentinelGridTelemetry",
        "sentinelgrid_telemetry_to_lambda",
        "sentinelgrid_telemetry_to_dynamodb",
        "sentinelgrid-anomaly-events-storage",
        "sentinelgrid-anomaly-detection-rule",
        "sentinelgrid-telemetry-storage",
        "sentinelgrid-anomaly-detector",
        "AnomalyDetectorExecutionRole",
        "AnomalyDetectorFunction",
    ]
    for name in existing_names:
        assert name not in raw, f"Template unexpectedly references existing resource/stack {name!r}"


def test_no_existing_infrastructure_template_was_modified_by_this_change():
    # This test only asserts the *other* templates still parse and still
    # declare exactly the resource types they always have — a coarse
    # tripwire, not a full content diff (this repo has no git history to
    # diff against). A real content change would very likely change the
    # resource-type set or fail to parse.
    expected_resource_types = {
        "telemetry-storage.yaml": {"AWS::DynamoDB::Table", "AWS::IAM::Role", "AWS::IoT::TopicRule"},
        "anomaly-detector.yaml": {"AWS::Lambda::Function", "AWS::IAM::Role"},
        "anomaly-detection-rule.yaml": {"AWS::Lambda::Permission", "AWS::IoT::TopicRule"},
        "anomaly-events-storage.yaml": {"AWS::DynamoDB::Table"},
    }
    for filename, expected_types in expected_resource_types.items():
        template = load_cfn_template(REPO_ROOT / "infrastructure" / filename)
        actual_types = {r["Type"] for r in template["Resources"].values()}
        assert actual_types == expected_types, f"{filename} resource types changed: {actual_types}"


# --- 7-12. IAM least-privilege checks ---


def _role_properties(template: dict) -> dict:
    return _resources_of_type(template, "AWS::IAM::Role")[0]["Properties"]


def test_role_trusts_only_states_service():
    props = _role_properties(_load_template())
    statements = props["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    assert statements[0]["Effect"] == "Allow"
    assert statements[0]["Principal"] == {"Service": "states.amazonaws.com"}
    assert statements[0]["Action"] == "sts:AssumeRole"


def test_role_grants_exactly_one_policy_with_exactly_lambda_invoke():
    # Step 18: the three remediation Lambdas now exist (see Step 17), so
    # this role grants exactly lambda:InvokeFunction on exactly those
    # three imported ARNs — no managed policies, no other actions.
    props = _role_properties(_load_template())
    assert "ManagedPolicyArns" not in props or props["ManagedPolicyArns"] == []
    policies = props.get("Policies", [])
    assert len(policies) == 1
    statement = policies[0]["PolicyDocument"]["Statement"]
    assert len(statement) == 1
    assert statement[0]["Effect"] == "Allow"
    assert statement[0]["Action"] == "lambda:InvokeFunction"


def test_invoke_resources_are_exactly_the_three_imported_lambda_arns():
    props = _role_properties(_load_template())
    statement = props["Policies"][0]["PolicyDocument"]["Statement"][0]
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert len(resources) == 3
    import_names = {r["Fn::ImportValue"] for r in resources if isinstance(r, dict) and "Fn::ImportValue" in r}
    assert import_names == {
        "SentinelGridRemediationPlannerArn",
        "SentinelGridRemediationExecutorArn",
        "SentinelGridRemediationVerifierArn",
    }


def test_no_wildcard_lambda_invoke_permission():
    props = _role_properties(_load_template())
    statement = props["Policies"][0]["PolicyDocument"]["Statement"][0]
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    for resource in resources:
        assert resource != "*"
        # Every resource must be an explicit ImportValue, never a literal
        # string (which could be a guessed/hardcoded ARN) and never a
        # wildcard/prefix pattern.
        assert isinstance(resource, dict) and "Fn::ImportValue" in resource


def test_no_wildcard_iam_resources_anywhere_in_template():
    raw = TEMPLATE_PATH.read_text()
    assert '"*"' not in raw
    assert "Resource: '*'" not in raw
    assert 'Resource: "*"' not in raw
    # A bare literal wildcard resource value, in any quoting style.
    assert not re.search(r"Resource:\s*\*\s*$", raw, re.MULTILINE)


def test_no_administrator_access_or_broad_managed_policy():
    raw = TEMPLATE_PATH.read_text()
    assert "AdministratorAccess" not in raw
    assert "PowerUserAccess" not in raw


def test_no_dynamodb_permissions():
    raw = TEMPLATE_PATH.read_text()
    assert "dynamodb:" not in raw


def test_no_iot_permissions():
    raw = TEMPLATE_PATH.read_text()
    assert "iot:" not in raw


def test_no_s3_permissions():
    raw = TEMPLATE_PATH.read_text()
    # s3:GetObject / s3:PutObject / etc. as an IAM Action — not simply the
    # word "S3" appearing in prose comments/descriptions.
    assert not re.search(r"\bs3:[A-Za-z]+\b", raw)


def test_no_sns_sqs_eventbridge_permissions():
    raw = TEMPLATE_PATH.read_text()
    for prefix in ("sns:", "sqs:", "events:"):
        assert prefix not in raw


# --- 13. Any Lambda permission is limited to lambda:InvokeFunction on explicit params ---


def test_only_lambda_action_anywhere_in_role_is_invokefunction():
    # Stronger than "InvokeFunction is present": no OTHER lambda:* action
    # (e.g. lambda:UpdateFunctionCode, lambda:*) appears anywhere in the
    # role's policy structure.
    props = _role_properties(_load_template())
    policies = props.get("Policies", [])
    for policy in policies:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            actions = actions if isinstance(actions, list) else [actions]
            for action in actions:
                if action.lower().startswith("lambda:"):
                    assert action == "lambda:InvokeFunction", f"unexpected Lambda permission: {action}"


# --- 14. No fake Lambda resources are created ---


def test_no_lambda_function_resources_created():
    template = _load_template()
    assert _resources_of_type(template, "AWS::Lambda::Function") == []


# --- 15. No EC2/ECS/SNS/SQS/etc. resources are created ---


def test_only_expected_resource_types_are_created():
    template = _load_template()
    actual_types = {r["Type"] for r in template["Resources"].values()}
    assert actual_types == {"AWS::IAM::Role", "AWS::StepFunctions::StateMachine"}
    for forbidden in FORBIDDEN_RESOURCE_TYPE_SUBSTRINGS:
        assert forbidden not in actual_types, f"Unexpected resource type present: {forbidden}"


# --- 16. Outputs expose StateMachine ARN and Execution Role ARN ---


def test_outputs_expose_state_machine_and_role_arns():
    template = _load_template()
    outputs = template["Outputs"]
    assert outputs["StateMachineArn"]["Value"] == {"Fn::GetAtt": ["RemediationWorkflowStateMachine", "Arn"]}
    assert outputs["ExecutionRoleArn"]["Value"] == {"Fn::GetAtt": ["RemediationWorkflowExecutionRole", "Arn"]}


# --- 17. Template descriptions are within AWS property limits ---


def test_top_level_description_is_within_1024_chars():
    template = _load_template()
    assert len(template["Description"]) <= 1024


# --- 18. Deployment script references correct account/profile/region/template/stack ---


def test_deploy_script_references_correct_conventions():
    script = DEPLOY_SCRIPT_PATH.read_text()
    assert 'AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"' in script
    assert 'AWS_REGION="${AWS_REGION:-ap-south-1}"' in script
    assert 'STACK_NAME="${STACK_NAME:-sentinelgrid-remediation-workflow}"' in script
    assert "remediation-workflow.yaml" in script
    assert "remediation_workflow.json" in script
    assert "CAPABILITY_NAMED_IAM" in script


def test_deploy_script_does_not_reference_other_stack_names():
    script = DEPLOY_SCRIPT_PATH.read_text()
    other_stacks = [
        "sentinelgrid-anomaly-detector",
        "sentinelgrid-anomaly-events-storage",
        "sentinelgrid-anomaly-detection-rule",
        "sentinelgrid-telemetry-storage",
    ]
    for stack in other_stacks:
        assert stack not in script


# --- 19. Existing infrastructure templates remain unchanged (file-level) ---


def test_existing_infrastructure_templates_still_exist_and_parse():
    for filename in EXISTING_INFRA_TEMPLATES:
        path = REPO_ROOT / "infrastructure" / filename
        assert path.exists(), f"{filename} is missing"
        load_cfn_template(path)  # must not raise
