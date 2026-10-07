#!/usr/bin/env python3
"""Step 47 — SentinelGrid security posture / least-privilege audit.

Read-only. Makes ONLY read/describe/list/get AWS API calls (IAM, Lambda,
DynamoDB, SQS, IoT, API Gateway v2, CloudWatch, SNS, S3, STS,
CloudTrail) — never a Put/Update/Delete/Start/Publish/Create/Modify
call of any kind. Never invokes a Lambda, never starts a Step
Functions execution, never writes to a table or queue, never publishes
a CloudWatch metric or SNS message.

This script collects EVIDENCE for `docs/security-audit.md`'s findings —
it does not itself decide "secure"/"insecure"; that judgment, with full
reasoning, lives in the markdown report. It never prints secret values;
env var KEYS are recorded, never VALUES containing credential-shaped
data (though in practice this project's Lambda env vars only ever hold
table/ARN names — see the report).

Usage:
    python3 scripts/step47_security_audit.py --output docs/step47_security_audit_results.json

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

ROLE_POLICIES = {
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

LAMBDA_FUNCTIONS = [
    "SentinelGridAnomalyDetector",
    "SentinelGridSqsTelemetryConsumer",
    "SentinelGridRemediationTrigger",
    "SentinelGridRemediationPlanner",
    "SentinelGridRemediationExecutor",
    "SentinelGridRemediationVerifier",
    "SentinelGridKnowledgeQuery",
]

DYNAMODB_TABLES = [
    "SentinelGridTelemetry",
    "SentinelGridAnomalyEvents",
    "SentinelGridRecoveryEvents",
    "SentinelGridNodeState",
]

SQS_QUEUES = {
    "SentinelGridTelemetryQueue": f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/SentinelGridTelemetryQueue",
    "SentinelGridTelemetryDLQ": f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/SentinelGridTelemetryDLQ",
}

API_ID = "m1uqr1hsq3"
DEPLOY_BUCKET = "sentinelgrid-lambda-deploy-401429382000"
STATE_MACHINE_ARN = f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:SentinelGridRemediationWorkflow"


def _session() -> boto3.Session:
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def audit_identity(session: boto3.Session) -> dict:
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    return {"UserId": identity["UserId"], "Account": identity["Account"], "Arn": identity["Arn"]}


def audit_iam(session: boto3.Session) -> dict:
    iam = session.client("iam")
    roles = {}
    for role_name, policy_names in ROLE_POLICIES.items():
        trust = iam.get_role(RoleName=role_name)["Role"]["AssumeRolePolicyDocument"]
        attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
        policies = {p: iam.get_role_policy(RoleName=role_name, PolicyName=p)["PolicyDocument"] for p in policy_names}
        roles[role_name] = {"trust_policy": trust, "attached_managed_policies": attached, "inline_policies": policies}
    return roles


def audit_wildcards(iam_roles: dict) -> dict:
    """Scans every collected policy statement for Action:"*" or an
    unconditioned/unexplained Resource:"*"."""
    findings = []
    for role_name, role in iam_roles.items():
        for policy_name, doc in role["inline_policies"].items():
            for stmt in doc.get("Statement", []):
                actions = stmt.get("Action", [])
                actions = [actions] if isinstance(actions, str) else actions
                resources = stmt.get("Resource", [])
                resources = [resources] if isinstance(resources, str) else resources
                if any(a == "*" for a in actions):
                    findings.append({"role": role_name, "policy": policy_name, "issue": "Action '*'", "statement": stmt})
                if "*" in resources:
                    findings.append(
                        {
                            "role": role_name,
                            "policy": policy_name,
                            "issue": "Resource '*'",
                            "actions": actions,
                            "has_condition": "Condition" in stmt,
                            "condition": stmt.get("Condition"),
                        }
                    )
    return {"wildcard_statements": findings}


def audit_lambda(session: boto3.Session) -> dict:
    lam = session.client("lambda")
    out = {}
    for fn in LAMBDA_FUNCTIONS:
        cfg = lam.get_function_configuration(FunctionName=fn)
        try:
            resource_policy = json.loads(lam.get_policy(FunctionName=fn)["Policy"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                resource_policy = None
            else:
                raise
        try:
            url_cfg = lam.get_function_url_config(FunctionName=fn)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                url_cfg = None
            else:
                raise
        out[fn] = {
            "Runtime": cfg.get("Runtime"),
            "Architectures": cfg.get("Architectures"),
            "Timeout": cfg.get("Timeout"),
            "MemorySize": cfg.get("MemorySize"),
            "EnvVarKeys": sorted((cfg.get("Environment") or {}).get("Variables", {}).keys()),
            "Role": cfg.get("Role"),
            "VpcConfig": cfg.get("VpcConfig"),
            "Layers": cfg.get("Layers"),
            "TracingConfig": cfg.get("TracingConfig"),
            "FunctionUrlConfig": url_cfg,
            "ResourceBasedPolicy": resource_policy,
            "LastModified": cfg.get("LastModified"),
        }
    return out


def audit_dynamodb(session: boto3.Session) -> dict:
    ddb = session.client("dynamodb")
    out = {}
    for table in DYNAMODB_TABLES:
        desc = ddb.describe_table(TableName=table)["Table"]
        pitr = ddb.describe_continuous_backups(TableName=table)["ContinuousBackupsDescription"]
        out[table] = {
            "BillingMode": desc.get("BillingModeSummary", {}).get("BillingMode"),
            "SSEDescription": desc.get("SSEDescription"),
            "DeletionProtectionEnabled": desc.get("DeletionProtectionEnabled"),
            "PointInTimeRecoveryStatus": pitr.get("PointInTimeRecoveryDescription", {}).get("PointInTimeRecoveryStatus"),
            "KeySchema": desc.get("KeySchema"),
        }
    return out


def audit_sqs(session: boto3.Session) -> dict:
    sqs = session.client("sqs")
    out = {}
    for name, url in SQS_QUEUES.items():
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
        out[name] = attrs
    return out


def audit_iot(session: boto3.Session) -> dict:
    iot = session.client("iot")
    out = {
        "policies": iot.list_policies().get("policies", []),
        "certificates": iot.list_certificates().get("certificates", []),
        "things": iot.list_things().get("things", []),
    }
    rules = iot.list_topic_rules().get("rules", [])
    out["topic_rules"] = rules
    out["topic_rule_details"] = {r["ruleName"]: iot.get_topic_rule(ruleName=r["ruleName"]).get("rule") for r in rules}
    return out


def audit_api_gateway(session: boto3.Session) -> dict:
    apigw = session.client("apigatewayv2")
    api = apigw.get_api(ApiId=API_ID)
    routes = apigw.get_routes(ApiId=API_ID)["Items"]
    stages = apigw.get_stages(ApiId=API_ID)["Items"]
    authorizers = apigw.get_authorizers(ApiId=API_ID).get("Items", [])
    return {
        "CorsConfiguration": api.get("CorsConfiguration"),
        "DisableExecuteApiEndpoint": api.get("DisableExecuteApiEndpoint"),
        "routes": [
            {"RouteKey": r["RouteKey"], "AuthorizationType": r["AuthorizationType"], "ApiKeyRequired": r["ApiKeyRequired"]}
            for r in routes
        ],
        "stages": [
            {"StageName": s["StageName"], "RouteSettings": s.get("RouteSettings"), "DefaultRouteSettings": s.get("DefaultRouteSettings")}
            for s in stages
        ],
        "authorizers": authorizers,
    }


def audit_cloudwatch_sns(session: boto3.Session) -> dict:
    cw = session.client("cloudwatch")
    sns = session.client("sns")
    alarms = cw.describe_alarms(AlarmNamePrefix="SentinelGrid")["MetricAlarms"]
    dashboards = cw.list_dashboards()["DashboardEntries"]
    topics = [t for t in sns.list_topics()["Topics"] if "SentinelGrid" in t["TopicArn"]]
    topic_details = {}
    for t in topics:
        arn = t["TopicArn"]
        topic_details[arn] = {
            "attributes": sns.get_topic_attributes(TopicArn=arn)["Attributes"],
            "subscriptions": sns.list_subscriptions_by_topic(TopicArn=arn)["Subscriptions"],
        }
    return {
        "alarms": [
            {
                "AlarmName": a["AlarmName"],
                "StateValue": a["StateValue"],
                "MetricName": a["MetricName"],
                "Namespace": a["Namespace"],
                "Dimensions": a["Dimensions"],
                "AlarmActions": a["AlarmActions"],
                "OKActions": a["OKActions"],
            }
            for a in alarms
        ],
        "dashboards": [d["DashboardName"] for d in dashboards],
        "sns_topics": topic_details,
    }


def audit_s3(session: boto3.Session) -> dict:
    s3 = session.client("s3")
    out = {}
    try:
        out["public_access_block"] = s3.get_public_access_block(Bucket=DEPLOY_BUCKET)["PublicAccessBlockConfiguration"]
    except ClientError as exc:
        out["public_access_block"] = f"ERROR: {exc.response['Error']['Code']}"
    try:
        out["bucket_policy"] = json.loads(s3.get_bucket_policy(Bucket=DEPLOY_BUCKET)["Policy"])
    except ClientError as exc:
        out["bucket_policy"] = f"ABSENT ({exc.response['Error']['Code']})"
    try:
        out["encryption"] = s3.get_bucket_encryption(Bucket=DEPLOY_BUCKET)["ServerSideEncryptionConfiguration"]
    except ClientError as exc:
        out["encryption"] = f"ERROR: {exc.response['Error']['Code']}"
    out["versioning"] = s3.get_bucket_versioning(Bucket=DEPLOY_BUCKET).get("Status", "Disabled")
    try:
        out["ownership"] = s3.get_bucket_ownership_controls(Bucket=DEPLOY_BUCKET)["OwnershipControls"]
    except ClientError as exc:
        out["ownership"] = f"ERROR: {exc.response['Error']['Code']}"
    return out


def audit_cloudtrail(session: boto3.Session) -> dict:
    ct = session.client("cloudtrail")
    trails = ct.describe_trails()["trailList"]
    event_data_stores = ct.list_event_data_stores().get("EventDataStores", [])
    default_history_sample = ct.lookup_events(MaxResults=1)["Events"]
    return {
        "trails": trails,
        "event_data_stores": event_data_stores,
        "default_event_history_available": len(default_history_sample) > 0,
    }


def audit_step_functions(session: boto3.Session) -> dict:
    sfn = session.client("stepfunctions")
    desc = sfn.describe_state_machine(stateMachineArn=STATE_MACHINE_ARN)
    definition = desc["definition"]
    import hashlib

    # .strip() before hashing: `aws ... --output text | shasum` (used
    # throughout this project's earlier steps) appends a trailing
    # newline that boto3's raw string does not have -- an artifact of
    # the CAPTURE METHOD, not of the deployed definition's actual
    # content, that otherwise produces a different hash for identical
    # content depending on which tool captured it. Stripping makes this
    # hash reproducible regardless of capture method. See
    # docs/security-audit.md's Step Functions ASL section for the full
    # investigation (three different hashes were observed for the same
    # underlying content across three capture methods before this was
    # understood).
    sha256_raw = hashlib.sha256(definition.encode("utf-8")).hexdigest()
    sha256_normalized = hashlib.sha256(definition.strip().encode("utf-8")).hexdigest()
    return {
        "name": desc["name"],
        "status": desc["status"],
        "live_definition_sha256_raw_boto3_string": sha256_raw,
        "live_definition_sha256_normalized": sha256_normalized,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=str(REPO_ROOT / "docs" / "step47_security_audit_results.json"))
    args = parser.parse_args()

    try:
        session = _session()
        identity = audit_identity(session)
    except (NoCredentialsError, ClientError, BotoCoreError) as exc:
        # BotoCoreError also covers ProfileNotFound (missing/misconfigured
        # profile) in addition to NoCredentialsError/ClientError (expired
        # or invalid session credentials) -- all are "AWS authentication
        # unavailable" per this project's standing rule to stop rather
        # than guess when identity cannot be verified.
        print(f"AWS authentication unavailable/expired: {exc}", file=sys.stderr)
        print(f"Manual reauthentication required (profile={PROFILE!r}, region={REGION!r}).", file=sys.stderr)
        sys.exit(2)

    print(f"Authenticated as: {identity['Arn']}")

    iam_roles = audit_iam(session)
    results = {
        "identity": identity,
        "iam_roles": iam_roles,
        "wildcard_audit": audit_wildcards(iam_roles),
        "lambda": audit_lambda(session),
        "dynamodb": audit_dynamodb(session),
        "sqs": audit_sqs(session),
        "iot": audit_iot(session),
        "api_gateway": audit_api_gateway(session),
        "cloudwatch_sns": audit_cloudwatch_sns(session),
        "s3_deployment_bucket": audit_s3(session),
        "cloudtrail": audit_cloudtrail(session),
        "step_functions": audit_step_functions(session),
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"Audit evidence written to: {output_path}")
    print(f"Wildcard statements found: {len(results['wildcard_audit']['wildcard_statements'])}")


if __name__ == "__main__":
    main()
