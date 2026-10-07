#!/usr/bin/env python3
"""Step 49 — SentinelGrid cost & resource audit.

STRICTLY read-only. Makes ONLY Describe/Get/List/HeadBucket/HeadObject
calls (CloudFormation, Lambda, DynamoDB, SQS, IoT, Step Functions,
API Gateway v2, CloudWatch, Logs, SNS, S3, IAM, STS, Service Quotas,
Cost Explorer). Never invokes a Lambda, never starts a Step Functions
execution, never sends/receives/deletes an SQS message, never publishes
telemetry or a CloudWatch metric, never publishes/subscribes an SNS
topic, never writes/deletes an S3 object, never changes IAM, never
changes a CloudFormation stack.

This script collects EVIDENCE for docs/cost-resource-audit.md -- it does
not itself decide cost amounts beyond what AWS's own Cost Explorer API
(if accessible) reports, and never fabricates a dollar figure.

Usage:
    python3 scripts/step49_cost_resource_audit.py --output docs/step49_cost_resource_audit_results.json

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

KNOWN_STACK_NAMES = [
    "sentinelgrid-telemetry-storage",
    "sentinelgrid-anomaly-detector",
    "sentinelgrid-anomaly-detection-rule",
    "sentinelgrid-anomaly-events-storage",
    "sentinelgrid-remediation-lambdas",
    "sentinelgrid-remediation-workflow",
    "sentinelgrid-remediation-trigger",
    "sentinelgrid-telemetry-sqs-queue",
    "sentinelgrid-telemetry-kinesis-stream",
    "sentinelgrid-sqs-consumer",
    "sentinelgrid-observability-dashboard",
    "sentinelgrid-observability-alerting",
    "sentinelgrid-recovery-events-storage",
    "sentinelgrid-node-state-storage",
    "sentinelgrid-knowledge-query",
    "sentinelgrid-frontend-hosting",
    "sentinelgrid-operator-identity",
]

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

SENTINELGRID_ROLE_NAMES = [
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
    "SentinelGridOperatorRole",
]


def _session() -> boto3.Session:
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def audit_identity(session: boto3.Session) -> dict:
    identity = session.client("sts").get_caller_identity()
    return {"UserId": identity["UserId"], "Account": identity["Account"], "Arn": identity["Arn"]}


def audit_lambda_quota(session: boto3.Session) -> dict:
    lam = session.client("lambda")
    try:
        settings = lam.get_account_settings()
        return {"ConcurrentExecutions_Limit": settings["AccountLimit"].get("ConcurrentExecutions")}
    except ClientError as exc:
        return {"error": exc.response["Error"]["Code"]}


def audit_cloudformation_stacks(session: boto3.Session) -> dict:
    cfn = session.client("cloudformation")
    out = {}
    for name in KNOWN_STACK_NAMES:
        try:
            desc = cfn.describe_stacks(StackName=name)["Stacks"][0]
            resources = cfn.list_stack_resources(StackName=name)["StackResourceSummaries"]
            out[name] = {
                "exists": True,
                "StackStatus": desc["StackStatus"],
                "CreationTime": str(desc.get("CreationTime")),
                "LastUpdatedTime": str(desc.get("LastUpdatedTime")) if desc.get("LastUpdatedTime") else None,
                "Outputs": desc.get("Outputs", []),
                "Resources": [
                    {"LogicalId": r["LogicalResourceId"], "Type": r["ResourceType"], "Status": r["ResourceStatus"]}
                    for r in resources
                ],
            }
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if "does not exist" in exc.response["Error"]["Message"]:
                out[name] = {"exists": False}
            else:
                out[name] = {"exists": "ERROR", "error": code}
    # Also list ALL stacks (any status) to catch anything not in the known list.
    try:
        all_stacks = cfn.list_stacks()["StackSummaries"]
        out["_all_stacks_in_account"] = [
            {"StackName": s["StackName"], "StackStatus": s["StackStatus"], "CreationTime": str(s.get("CreationTime"))}
            for s in all_stacks
        ]
    except ClientError as exc:
        out["_all_stacks_in_account"] = f"ERROR: {exc.response['Error']['Code']}"
    return out


def audit_lambda(session: boto3.Session) -> dict:
    lam = session.client("lambda")
    out = {}
    for fn in LAMBDA_FUNCTIONS:
        try:
            cfg = lam.get_function(FunctionName=fn)
            code = cfg.get("Code", {})
            configuration = cfg["Configuration"]
            try:
                concurrency = lam.get_function_concurrency(FunctionName=fn)
            except ClientError:
                concurrency = {}
            try:
                url_cfg = lam.get_function_url_config(FunctionName=fn)
            except ClientError as exc:
                url_cfg = None if exc.response["Error"]["Code"] == "ResourceNotFoundException" else f"ERROR: {exc.response['Error']['Code']}"
            out[fn] = {
                "Runtime": configuration.get("Runtime"),
                "Architectures": configuration.get("Architectures"),
                "MemorySize": configuration.get("MemorySize"),
                "Timeout": configuration.get("Timeout"),
                "CodeSize": configuration.get("CodeSize"),
                "LastModified": configuration.get("LastModified"),
                "ReservedConcurrentExecutions": concurrency.get("ReservedConcurrentExecutions"),
                "FunctionUrlConfig": url_cfg,
                "PackageType": configuration.get("PackageType"),
                "LogGroup": f"/aws/lambda/{fn}",
            }
        except ClientError as exc:
            out[fn] = {"error": exc.response["Error"]["Code"]}
    return out


def audit_dynamodb(session: boto3.Session) -> dict:
    ddb = session.client("dynamodb")
    out = {}
    for table in DYNAMODB_TABLES:
        try:
            desc = ddb.describe_table(TableName=table)["Table"]
            pitr = ddb.describe_continuous_backups(TableName=table)["ContinuousBackupsDescription"]
            out[table] = {
                "BillingMode": desc.get("BillingModeSummary", {}).get("BillingMode"),
                "ProvisionedThroughput": desc.get("ProvisionedThroughput"),
                "ItemCount": desc.get("ItemCount"),
                "TableSizeBytes": desc.get("TableSizeBytes"),
                "SSEDescription": desc.get("SSEDescription"),
                "DeletionProtectionEnabled": desc.get("DeletionProtectionEnabled"),
                "PointInTimeRecoveryStatus": pitr.get("PointInTimeRecoveryDescription", {}).get("PointInTimeRecoveryStatus"),
                "TableArn": desc.get("TableArn"),
                "TableStatus": desc.get("TableStatus"),
                # ItemCount/TableSizeBytes are updated by DynamoDB approximately
                # every 6 hours -- documented explicitly so the report does not
                # overstate precision.
                "_note": "ItemCount and TableSizeBytes are DynamoDB's own periodic estimates (updated roughly every 6 hours), not a live COUNT.",
            }
        except ClientError as exc:
            out[table] = {"error": exc.response["Error"]["Code"]}
    return out


def audit_sqs(session: boto3.Session) -> dict:
    sqs = session.client("sqs")
    out = {}
    for name, url in SQS_QUEUES.items():
        try:
            attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
            out[name] = attrs
        except ClientError as exc:
            out[name] = {"error": exc.response["Error"]["Code"]}
    return out


def audit_iot(session: boto3.Session) -> dict:
    iot = session.client("iot")
    rules = iot.list_topic_rules().get("rules", [])
    return {
        "topic_rules": rules,
        "topic_rule_details": {r["ruleName"]: iot.get_topic_rule(ruleName=r["ruleName"]).get("rule") for r in rules},
        "policies": iot.list_policies().get("policies", []),
        "certificates": iot.list_certificates().get("certificates", []),
        "things": iot.list_things().get("things", []),
    }


def audit_step_functions(session: boto3.Session) -> dict:
    sfn = session.client("stepfunctions")
    desc = sfn.describe_state_machine(stateMachineArn=STATE_MACHINE_ARN)
    executions = sfn.list_executions(stateMachineArn=STATE_MACHINE_ARN, maxResults=1000)["executions"]
    status_counts = {}
    for e in executions:
        status_counts[e["status"]] = status_counts.get(e["status"], 0) + 1
    return {
        "name": desc["name"],
        "status": desc["status"],
        "type": desc.get("type"),
        "loggingConfiguration": desc.get("loggingConfiguration"),
        "tracingConfiguration": desc.get("tracingConfiguration"),
        "total_executions_listed": len(executions),
        "execution_status_counts": status_counts,
        "_note": "listExecutions capped at maxResults=1000; total_executions_listed may undercount if more than 1000 exist.",
    }


def audit_api_gateway(session: boto3.Session) -> dict:
    apigw = session.client("apigatewayv2")
    api = apigw.get_api(ApiId=API_ID)
    routes = apigw.get_routes(ApiId=API_ID)["Items"]
    stages = apigw.get_stages(ApiId=API_ID)["Items"]
    return {
        "ApiEndpoint": api.get("ApiEndpoint"),
        "ProtocolType": api.get("ProtocolType"),
        "routes_count": len(routes),
        "stages": [
            {"StageName": s["StageName"], "AutoDeploy": s.get("AutoDeploy"), "RouteSettings": s.get("RouteSettings"), "DefaultRouteSettings": s.get("DefaultRouteSettings")}
            for s in stages
        ],
    }


def audit_cloudwatch(session: boto3.Session) -> dict:
    cw = session.client("cloudwatch")
    logs = session.client("logs")
    out = {}
    out["alarms"] = [
        {"AlarmName": a["AlarmName"], "StateValue": a["StateValue"]}
        for a in cw.describe_alarms(AlarmNamePrefix="SentinelGrid")["MetricAlarms"]
    ]
    out["dashboards"] = [d["DashboardName"] for d in cw.list_dashboards()["DashboardEntries"]]
    try:
        metrics = cw.list_metrics(Namespace="SentinelGrid")["Metrics"]
        out["custom_metric_count"] = len(metrics)
        out["custom_metric_names"] = sorted({m["MetricName"] for m in metrics})
    except ClientError as exc:
        out["custom_metrics"] = f"ERROR: {exc.response['Error']['Code']}"

    log_groups = {}
    for fn in LAMBDA_FUNCTIONS:
        lg_name = f"/aws/lambda/{fn}"
        try:
            resp = logs.describe_log_groups(logGroupNamePrefix=lg_name)
            matches = [g for g in resp.get("logGroups", []) if g["logGroupName"] == lg_name]
            if matches:
                g = matches[0]
                log_groups[lg_name] = {
                    "exists": True,
                    "retentionInDays": g.get("retentionInDays"),
                    "storedBytes": g.get("storedBytes"),
                    "retention_is_never_expire": "retentionInDays" not in g,
                }
            else:
                log_groups[lg_name] = {"exists": False}
        except ClientError as exc:
            log_groups[lg_name] = {"error": exc.response["Error"]["Code"]}
    out["lambda_log_groups"] = log_groups
    return out


def audit_sns(session: boto3.Session) -> dict:
    sns = session.client("sns")
    topics = [t for t in sns.list_topics()["Topics"] if "SentinelGrid" in t["TopicArn"]]
    out = {}
    for t in topics:
        arn = t["TopicArn"]
        out[arn] = {
            "subscriptions": sns.list_subscriptions_by_topic(TopicArn=arn)["Subscriptions"],
        }
    return out


def audit_s3(session: boto3.Session) -> dict:
    s3 = session.client("s3")
    out = {}
    try:
        loc = s3.get_bucket_location(Bucket=DEPLOY_BUCKET)
        out["region"] = loc.get("LocationConstraint")
    except ClientError as exc:
        out["region"] = f"ERROR: {exc.response['Error']['Code']}"

    out["versioning"] = s3.get_bucket_versioning(Bucket=DEPLOY_BUCKET).get("Status", "Disabled")

    try:
        paginator = s3.get_paginator("list_object_versions")
        current_count, noncurrent_count, delete_markers, total_bytes = 0, 0, 0, 0
        for page in paginator.paginate(Bucket=DEPLOY_BUCKET):
            for v in page.get("Versions", []):
                total_bytes += v.get("Size", 0)
                if v.get("IsLatest"):
                    current_count += 1
                else:
                    noncurrent_count += 1
            delete_markers += len(page.get("DeleteMarkers", []))
        out["object_versions"] = {
            "current_versions": current_count,
            "noncurrent_versions": noncurrent_count,
            "delete_markers": delete_markers,
            "total_bytes_all_versions": total_bytes,
        }
    except ClientError as exc:
        out["object_versions"] = f"ERROR: {exc.response['Error']['Code']}"

    try:
        mpu = s3.list_multipart_uploads(Bucket=DEPLOY_BUCKET)
        out["incomplete_multipart_uploads"] = len(mpu.get("Uploads", []))
    except ClientError as exc:
        out["incomplete_multipart_uploads"] = f"ERROR: {exc.response['Error']['Code']}"

    try:
        out["lifecycle_rules"] = s3.get_bucket_lifecycle_configuration(Bucket=DEPLOY_BUCKET).get("Rules", [])
    except ClientError as exc:
        out["lifecycle_rules"] = f"ABSENT ({exc.response['Error']['Code']})"

    return out


def audit_iam(session: boto3.Session) -> dict:
    iam = session.client("iam")
    out = {}
    for role_name in SENTINELGRID_ROLE_NAMES:
        try:
            role = iam.get_role(RoleName=role_name)["Role"]
            policy_names = iam.list_role_policies(RoleName=role_name)["PolicyNames"]
            attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
            out[role_name] = {
                "CreateDate": str(role["CreateDate"]),
                "inline_policy_count": len(policy_names),
                "attached_managed_policy_count": len(attached),
            }
        except ClientError as exc:
            out[role_name] = {"error": exc.response["Error"]["Code"]}
    return out


def audit_cost_explorer(session: boto3.Session) -> dict:
    """Cost Explorer is a us-east-1 global endpoint regardless of the
    working region. Access is frequently restricted in
    student/sandbox accounts -- this function reports that clearly
    rather than guessing."""
    try:
        ce = session.client("ce", region_name="us-east-1")
        import datetime

        today = datetime.date.today()
        first_of_this_month = today.replace(day=1)
        first_of_last_month = (first_of_this_month - datetime.timedelta(days=1)).replace(day=1)
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": str(first_of_last_month), "End": str(today + datetime.timedelta(days=1))},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        return {"accessible": True, "ResultsByTime": resp.get("ResultsByTime", [])}
    except (ClientError, BotoCoreError) as exc:
        return {"accessible": False, "error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=str(REPO_ROOT / "docs" / "step49_cost_resource_audit_results.json"))
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
        "lambda_account_quota": audit_lambda_quota(session),
        "cloudformation_stacks": audit_cloudformation_stacks(session),
        "lambda": audit_lambda(session),
        "dynamodb": audit_dynamodb(session),
        "sqs": audit_sqs(session),
        "iot": audit_iot(session),
        "step_functions": audit_step_functions(session),
        "api_gateway": audit_api_gateway(session),
        "cloudwatch": audit_cloudwatch(session),
        "sns": audit_sns(session),
        "s3_deployment_bucket": audit_s3(session),
        "iam_roles_summary": audit_iam(session),
        "cost_explorer": audit_cost_explorer(session),
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"Audit evidence written to: {output_path}")
    print(f"Cost Explorer accessible: {results['cost_explorer']['accessible']}")


if __name__ == "__main__":
    main()
