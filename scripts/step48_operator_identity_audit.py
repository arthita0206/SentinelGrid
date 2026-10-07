#!/usr/bin/env python3
"""Step 48 — SentinelGrid operator-identity verification.

Read-only. Makes ONLY Get/List/Describe AWS API calls (STS, IAM,
sso-admin, Organizations, CloudFormation) plus exactly ONE
non-mutating capability probe: an `sts:assume_role` call against
SentinelGridOperatorRole, which either succeeds (returning temporary
credentials that are discarded, never printed, never used for a
follow-up call in this script) or fails with AccessDenied — both
outcomes are informational evidence, not a mutation, since AssumeRole
creates no persistent AWS resource.

This script never creates/updates/deletes an IAM user, access key,
role, or policy. It never touches account settings, billing, MFA, or
root credentials.

Usage:
    python3 scripts/step48_operator_identity_audit.py --output docs/step48_operator_identity_results.json

Exits non-zero with a clear message if AWS authentication is
unavailable/expired, per this project's standing rule to stop rather
than guess.
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import boto3  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError  # noqa: E402

PROFILE = "sentinelgrid"
REGION = "ap-south-1"
ACCOUNT_ID = "401429382000"
OPERATOR_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/SentinelGridOperatorRole"

# The same 10 service-execution-role -> inline-policy-name mapping used by
# scripts/step47_security_audit.py, so this script can independently
# re-confirm none of them changed as a side effect of Step 48.
SERVICE_ROLE_POLICIES = {
    "SentinelGridAnomalyDetectorExecutionRole": [
        "SentinelGridAnomalyDetectorAnomalyEventsWrite",
        "SentinelGridAnomalyDetectorInvokeRemediationTrigger",
        "SentinelGridAnomalyDetectorLogging",
        "SentinelGridAnomalyDetectorNodeStateWrite",
        "SentinelGridAnomalyDetectorPublishMetrics",
    ],
    "SentinelGridIoTToDynamoDBRole": ["SentinelGridIoTDynamoDBWrite"],
    "SentinelGridIoTToSqsRole": ["SentinelGridIoTSqsSendMessage"],
    "SentinelGridKnowledgeQueryExecutionRole": [
        "SentinelGridKnowledgeQueryAnomalyEventsRead",
        "SentinelGridKnowledgeQueryLogging",
        "SentinelGridKnowledgeQueryMetricsRead",
        "SentinelGridKnowledgeQueryNodeStateRead",
        "SentinelGridKnowledgeQueryRecoveryEventsRead",
    ],
    "SentinelGridRemediationExecutorRole": [
        "SentinelGridRemediationExecutorLogging",
        "SentinelGridRemediationExecutorNodeStateWrite",
        "SentinelGridRemediationExecutorPublishMetrics",
        "SentinelGridRemediationExecutorRecoveryEventsAccess",
    ],
    "SentinelGridRemediationPlannerRole": [
        "SentinelGridRemediationPlannerLogging",
        "SentinelGridRemediationPlannerNodeStateWrite",
        "SentinelGridRemediationPlannerPublishMetrics",
        "SentinelGridRemediationPlannerRecoveryEventsAccess",
    ],
    "SentinelGridRemediationTriggerExecutionRole": [
        "SentinelGridRemediationTriggerLogging",
        "SentinelGridRemediationTriggerStartExecution",
    ],
    "SentinelGridRemediationVerifierRole": [
        "SentinelGridRemediationVerifierLogging",
        "SentinelGridRemediationVerifierNodeStateWrite",
        "SentinelGridRemediationVerifierPublishMetrics",
    ],
    "SentinelGridRemediationWorkflowRole": ["SentinelGridRemediationWorkflowInvokeLambdas"],
    "SentinelGridSqsConsumerExecutionRole": [
        "SentinelGridSqsConsumerInvokeAnalyzer",
        "SentinelGridSqsConsumerLogging",
        "SentinelGridSqsConsumerPollQueue",
    ],
}


def _session() -> boto3.Session:
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def audit_identity(session: boto3.Session) -> dict:
    identity = session.client("sts").get_caller_identity()
    return {"UserId": identity["UserId"], "Account": identity["Account"], "Arn": identity["Arn"]}


def audit_account_identity_landscape(session: boto3.Session) -> dict:
    iam = session.client("iam")
    out = {}
    try:
        out["account_aliases"] = iam.list_account_aliases()["AccountAliases"]
    except ClientError as exc:
        out["account_aliases"] = f"ERROR: {exc.response['Error']['Code']}"
    try:
        out["iam_users"] = [
            {"UserName": u["UserName"], "Arn": u["Arn"], "CreateDate": str(u["CreateDate"])}
            for u in iam.list_users()["Users"]
        ]
    except ClientError as exc:
        out["iam_users"] = f"ERROR: {exc.response['Error']['Code']}"
    try:
        all_roles = iam.list_roles()["Roles"]
        out["non_sentinelgrid_non_service_linked_roles"] = [
            r["RoleName"]
            for r in all_roles
            if not r["RoleName"].startswith("SentinelGrid") and "/aws-service-role/" not in r["Arn"]
        ]
    except ClientError as exc:
        out["non_sentinelgrid_non_service_linked_roles"] = f"ERROR: {exc.response['Error']['Code']}"
    try:
        sso = session.client("sso-admin")
        out["sso_instances"] = sso.list_instances().get("Instances", [])
    except (ClientError, BotoCoreError) as exc:
        out["sso_instances"] = f"ERROR: {exc}"
    try:
        org = session.client("organizations")
        out["organizations_membership"] = org.describe_organization().get("Organization", {})
    except ClientError as exc:
        out["organizations_membership"] = f"NOT A MEMBER: {exc.response['Error']['Code']}"
    return out


def audit_operator_role(session: boto3.Session) -> dict:
    iam = session.client("iam")
    role = iam.get_role(RoleName="SentinelGridOperatorRole")["Role"]
    policy_names = iam.list_role_policies(RoleName="SentinelGridOperatorRole")["PolicyNames"]
    policies = {p: iam.get_role_policy(RoleName="SentinelGridOperatorRole", PolicyName=p)["PolicyDocument"] for p in policy_names}
    return {
        "RoleName": role["RoleName"],
        "Arn": role["Arn"],
        "CreateDate": str(role["CreateDate"]),
        "MaxSessionDuration": role.get("MaxSessionDuration"),
        "AssumeRolePolicyDocument": role["AssumeRolePolicyDocument"],
        "inline_policy_names": policy_names,
        "inline_policies": policies,
    }


def probe_assume_role(session: boto3.Session) -> dict:
    """Attempts sts:AssumeRole against SentinelGridOperatorRole. This is
    NOT a mutation -- STS AssumeRole creates no persistent resource,
    only (on success) temporary credentials, which this function
    discards immediately and never logs, prints, or reuses. Both
    outcomes (success or AccessDenied) are meaningful evidence for
    whether the current identity can use the role."""
    sts = session.client("sts")
    try:
        sts.assume_role(RoleArn=OPERATOR_ROLE_ARN, RoleSessionName="step48-verification-probe")
        return {"outcome": "SUCCEEDED", "note": "Temporary credentials were obtained and immediately discarded; never printed."}
    except ClientError as exc:
        return {
            "outcome": "DENIED",
            "error_code": exc.response["Error"]["Code"],
            "error_message": exc.response["Error"]["Message"],
        }


def audit_service_role_regression(session: boto3.Session, prior_evidence_path: Path) -> dict:
    """Re-fetches the 10 SentinelGrid service-execution roles live and
    diffs them against Step 47's saved evidence to confirm Step 48 did
    not alter any of them."""
    if not prior_evidence_path.exists():
        return {"checked": False, "reason": f"{prior_evidence_path} not found"}
    with open(prior_evidence_path) as f:
        prior = json.load(f)["iam_roles"]
    iam = session.client("iam")
    changed = []
    for role_name, policy_names in SERVICE_ROLE_POLICIES.items():
        live_trust = iam.get_role(RoleName=role_name)["Role"]["AssumeRolePolicyDocument"]
        live_inline = {p: iam.get_role_policy(RoleName=role_name, PolicyName=p)["PolicyDocument"] for p in policy_names}
        prior_data = prior.get(role_name, {})
        if live_trust != prior_data.get("trust_policy") or live_inline != prior_data.get("inline_policies"):
            changed.append(role_name)
    return {"checked": True, "roles_checked": list(SERVICE_ROLE_POLICIES.keys()), "roles_changed": changed}


def audit_operator_stack(session: boto3.Session) -> dict:
    cfn = session.client("cloudformation")
    try:
        stacks = cfn.describe_stacks(StackName="sentinelgrid-operator-identity")["Stacks"]
        s = stacks[0]
        return {
            "StackStatus": s["StackStatus"],
            "Outputs": s.get("Outputs", []),
            "CreationTime": str(s["CreationTime"]),
        }
    except ClientError as exc:
        return {"error": exc.response["Error"]["Code"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=str(REPO_ROOT / "docs" / "step48_operator_identity_results.json"))
    args = parser.parse_args()

    try:
        session = _session()
        identity = audit_identity(session)
    except (NoCredentialsError, ClientError, BotoCoreError) as exc:
        print(f"AWS authentication unavailable/expired: {exc}", file=sys.stderr)
        print(f"Manual reauthentication required (profile={PROFILE!r}, region={REGION!r}).", file=sys.stderr)
        print("Run: aws login --profile sentinelgrid --region ap-south-1", file=sys.stderr)
        sys.exit(2)

    print(f"Authenticated as: {identity['Arn']}")

    results = {
        "identity": identity,
        "account_identity_landscape": audit_account_identity_landscape(session),
        "operator_role": audit_operator_role(session),
        "operator_stack": audit_operator_stack(session),
        "assume_role_probe": probe_assume_role(session),
        "service_role_regression": audit_service_role_regression(session, REPO_ROOT / "docs" / "step47_security_audit_results.json"),
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"Audit evidence written to: {output_path}")
    print(f"AssumeRole probe outcome: {results['assume_role_probe']['outcome']}")
    print(f"Service roles changed: {results['service_role_regression'].get('roles_changed')}")


if __name__ == "__main__":
    main()
