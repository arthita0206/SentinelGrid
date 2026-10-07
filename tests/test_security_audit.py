"""Tests for the Step 47 security audit script (scripts/step47_security_audit.py).

These tests exercise the script's pure logic (wildcard detection, module
constants, JSON output shape) without requiring live AWS credentials, so
they run as part of the normal `pytest` suite in any environment. They do
NOT call AWS -- the script's AWS-calling functions (audit_iam,
audit_lambda, audit_dynamodb, etc.) are exercised only by actually running
the script against a real account, which is out of scope for unit tests
and was done manually as part of Step 47 (see docs/security-audit.md).
"""

import json
import subprocess
import sys
from pathlib import Path

from scripts.step47_security_audit import (
    DYNAMODB_TABLES,
    LAMBDA_FUNCTIONS,
    ROLE_POLICIES,
    SQS_QUEUES,
    audit_wildcards,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_role_policies_covers_all_known_lambda_and_service_roles():
    # Every SentinelGrid Lambda role, plus the IoT and Step Functions
    # roles, must be present -- this is the audit's role coverage list.
    expected_roles = {
        "SentinelGridAnomalyDetectorExecutionRole",
        "SentinelGridIoTToDynamoDBRole",
        "SentinelGridIoTToSqsRole",
        "SentinelGridKnowledgeQueryExecutionRole",
        "SentinelGridRemediationExecutorRole",
        "SentinelGridRemediationPlannerRole",
        "SentinelGridRemediationTriggerExecutionRole",
        "SentinelGridRemediationVerifierRole",
        "SentinelGridRemediationWorkflowRole",
        "SentinelGridSqsConsumerExecutionRole",
    }
    assert set(ROLE_POLICIES.keys()) == expected_roles


def test_lambda_functions_list_has_seven_known_functions():
    assert len(LAMBDA_FUNCTIONS) == 7
    assert "SentinelGridKnowledgeQuery" in LAMBDA_FUNCTIONS
    assert "SentinelGridAnomalyDetector" in LAMBDA_FUNCTIONS


def test_dynamodb_tables_list_has_four_known_tables():
    assert set(DYNAMODB_TABLES) == {
        "SentinelGridTelemetry",
        "SentinelGridAnomalyEvents",
        "SentinelGridRecoveryEvents",
        "SentinelGridNodeState",
    }


def test_sqs_queues_map_has_main_queue_and_dlq():
    assert set(SQS_QUEUES.keys()) == {"SentinelGridTelemetryQueue", "SentinelGridTelemetryDLQ"}
    for url in SQS_QUEUES.values():
        assert url.startswith("https://sqs.ap-south-1.amazonaws.com/401429382000/")


def test_audit_wildcards_flags_unconditioned_resource_star():
    fake_roles = {
        "SomeRole": {
            "inline_policies": {
                "SomePolicy": {
                    "Statement": [
                        {"Effect": "Allow", "Action": ["cloudwatch:GetMetricData"], "Resource": "*"}
                    ]
                }
            }
        }
    }
    findings = audit_wildcards(fake_roles)["wildcard_statements"]
    assert len(findings) == 1
    assert findings[0]["role"] == "SomeRole"
    assert findings[0]["has_condition"] is False


def test_audit_wildcards_flags_conditioned_resource_star_but_records_condition():
    fake_roles = {
        "SomeRole": {
            "inline_policies": {
                "SomePolicy": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["cloudwatch:PutMetricData"],
                            "Resource": "*",
                            "Condition": {"StringEquals": {"cloudwatch:namespace": "SentinelGrid"}},
                        }
                    ]
                }
            }
        }
    }
    findings = audit_wildcards(fake_roles)["wildcard_statements"]
    assert len(findings) == 1
    assert findings[0]["has_condition"] is True
    assert findings[0]["condition"] == {"StringEquals": {"cloudwatch:namespace": "SentinelGrid"}}


def test_audit_wildcards_flags_action_star():
    fake_roles = {
        "SomeRole": {
            "inline_policies": {
                "SomePolicy": {
                    "Statement": [
                        {"Effect": "Allow", "Action": "*", "Resource": ["arn:aws:dynamodb:...:table/Foo"]}
                    ]
                }
            }
        }
    }
    findings = audit_wildcards(fake_roles)["wildcard_statements"]
    assert len(findings) == 1
    assert findings[0]["issue"] == "Action '*'"


def test_audit_wildcards_no_false_positive_on_fully_scoped_policy():
    fake_roles = {
        "SomeRole": {
            "inline_policies": {
                "SomePolicy": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["dynamodb:PutItem"],
                            "Resource": ["arn:aws:dynamodb:ap-south-1:401429382000:table/SentinelGridTelemetry"],
                        }
                    ]
                }
            }
        }
    }
    findings = audit_wildcards(fake_roles)["wildcard_statements"]
    assert findings == []


def test_audit_wildcards_handles_multiple_roles_and_policies():
    fake_roles = {
        "RoleA": {
            "inline_policies": {
                "PolicyA1": {"Statement": [{"Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::bucket/*"]}]},
                "PolicyA2": {"Statement": [{"Action": ["logs:PutLogEvents"], "Resource": "*"}]},
            }
        },
        "RoleB": {
            "inline_policies": {
                "PolicyB1": {"Statement": [{"Action": ["sqs:SendMessage"], "Resource": ["arn:aws:sqs:...:Q"]}]},
            }
        },
    }
    findings = audit_wildcards(fake_roles)["wildcard_statements"]
    assert len(findings) == 1
    assert findings[0]["role"] == "RoleA"
    assert findings[0]["policy"] == "PolicyA2"


def test_script_exits_nonzero_with_clear_message_when_aws_unavailable(tmp_path, monkeypatch):
    """The audit script must fail clearly (not hang, not crash with a
    traceback dump) when AWS credentials/profile are unavailable, per the
    project's standing 'stop rather than guess' rule."""
    env = {
        "PATH": "/usr/bin:/bin",  # deny access to the real aws CLI config lookup shortcuts
        "AWS_PROFILE": "this-profile-does-not-exist-step47-test",
        "AWS_DEFAULT_REGION": "ap-south-1",
        "AWS_CONFIG_FILE": str(tmp_path / "nonexistent-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "nonexistent-credentials"),
    }
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "step47_security_audit.py"), "--output", str(tmp_path / "out.json")],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "authentication" in result.stderr.lower() or "credentials" in result.stderr.lower()
    assert not (tmp_path / "out.json").exists()


def test_step47_security_audit_results_json_is_well_formed_if_present():
    """If the evidence file from a live audit run exists in the repo, it
    must be valid JSON with the top-level sections the report depends on.
    This does not require AWS access itself -- it only validates the
    shape of evidence already captured."""
    results_path = REPO_ROOT / "docs" / "step47_security_audit_results.json"
    if not results_path.exists():
        return  # nothing to validate in an environment without a prior live run
    with open(results_path) as f:
        data = json.load(f)
    expected_sections = {
        "identity",
        "iam_roles",
        "wildcard_audit",
        "lambda",
        "dynamodb",
        "sqs",
        "iot",
        "api_gateway",
        "cloudwatch_sns",
        "s3_deployment_bucket",
        "cloudtrail",
        "step_functions",
    }
    assert expected_sections.issubset(data.keys())
    sfn = data["step_functions"]
    assert "live_definition_sha256_raw_boto3_string" in sfn
    assert "live_definition_sha256_normalized" in sfn
