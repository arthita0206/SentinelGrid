# Infrastructure

Infrastructure-as-Code for SentinelGrid's AWS resources, written as plain
CloudFormation (no CDK/Terraform dependency needed for this project's
scope). **Nothing here is deployed automatically** — templates are applied
manually, deliberately, via the AWS CLI or the helper script below.

## telemetry-storage.yaml (current)

Creates the persistent storage path for telemetry that `AwsIotPublisher`
(see [../simulator/aws_publisher.py](../simulator/aws_publisher.py)) is
already publishing to AWS IoT Core:

```
CitySimulator -> FaultInjectionEngine -> AwsIotPublisher
    -> AWS IoT Core (topic: sentinelgrid/telemetry)
    -> IoT Topic Rule: sentinelgrid_telemetry_to_dynamodb
    -> DynamoDB table: SentinelGridTelemetry
```

### Resources created

| Resource | Type | Purpose |
|---|---|---|
| `TelemetryTable` | `AWS::DynamoDB::Table` (`SentinelGridTelemetry`) | Stores every telemetry reading. `PAY_PER_REQUEST` billing — no provisioned capacity to size or pay for while idle. |
| `IoTToDynamoDBRole` | `AWS::IAM::Role` (`SentinelGridIoTToDynamoDBRole`) | Dedicated role AWS IoT Core assumes to write to the table. Trust policy allows only `iot.amazonaws.com`; its one inline policy grants only `dynamodb:PutItem`, scoped to the `SentinelGridTelemetry` table's ARN — no `*` resources, no admin access. |
| `TelemetryToDynamoDBRule` | `AWS::IoT::TopicRule` (`sentinelgrid_telemetry_to_dynamodb`) | Subscribes to the `sentinelgrid/telemetry` MQTT topic and writes each message into the table via the role above. |

### DynamoDB key schema

- **Partition key**: `node_id` (String) — e.g. `power-001`, `water-014`, `traffic-007`.
- **Sort key**: `timestamp` (String) — the UTC ISO-8601 timestamp already present on every `Telemetry` reading (see [../shared/telemetry.py](../shared/telemetry.py)).

Together they let every reading from a given node be queried in
chronological order, while keeping readings from different nodes
naturally partitioned. This matches the telemetry payload's own shape —
no transformation is needed between what the simulator publishes and what
gets stored.

### How the rule connects to DynamoDB

The rule's SQL (`SELECT * FROM 'sentinelgrid/telemetry'`, `AwsIotSqlVersion:
2016-03-23`) matches every message on the topic and selects the entire
payload — `node_id`, `subsystem`, `timestamp`, `status`, and the nested
`data` object all pass through unchanged (DynamoDB stores `data` as a
native Map).

The rule uses the **`DynamoDBv2`** action rather than the older `DynamoDB`
action. `DynamoDBv2` writes the *entire* selected payload as one item and
derives the partition/sort key values directly from whichever top-level
message fields share their names with the table's key schema — which is
exactly `node_id` and `timestamp` here. That's why this table's keys were
chosen to match the payload's own field names: no per-field key mapping,
string templating, or Lambda transform is needed to get a reading from
MQTT into DynamoDB.

### Future use (not implemented yet)

This table is the foundation the next steps will build on:

- An **Analyzer** (Lambda, triggered by DynamoDB Streams or scheduled) can
  read recent readings per node to detect anomalies — the FR-2.x faults
  already simulated (dropout, corruption, partition, overload) are what
  it will eventually need to notice.
- A **Planner/Executor** could later query this table to compare a node's
  current and historical readings before deciding on a remediation
  action.
- A **dashboard** could query this table directly for recent/historical
  telemetry per node or subsystem.

None of that is implemented by this template — it only creates the
storage and ingestion path.

### What this step does NOT do

- No Lambda functions, Step Functions, CloudWatch alarms, SNS topics, or
  DynamoDB Streams consumers are created.
- No anomaly detection or remediation logic.
- Nothing in this template is deployed automatically — see below.

## Deploying

Nothing is deployed by writing or committing this template. To deploy it
yourself:

```bash
./infrastructure/deploy-telemetry-storage.sh
```

This script embeds no credentials — it uses the AWS CLI's normal profile
resolution, defaulting to `AWS_PROFILE=sentinelgrid` and
`AWS_REGION=ap-south-1` (SentinelGrid's development profile/region),
both overridable via environment variables. `CAPABILITY_NAMED_IAM` is
required because the template creates a named IAM role.

Or run the equivalent AWS CLI command directly:

```bash
aws cloudformation deploy \
  --stack-name sentinelgrid-telemetry-storage \
  --template-file infrastructure/telemetry-storage.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile sentinelgrid \
  --region ap-south-1
```

### Before you deploy (manual prerequisites)

- The `sentinelgrid` AWS CLI profile must be authenticated (e.g. via
  `aws login` / `aws configure`), matching what
  [`AwsIotPublisher`](../simulator/aws_publisher.py) already uses.
- That identity needs permission to create DynamoDB tables, IAM roles, and
  IoT topic rules (`dynamodb:CreateTable`, `iam:CreateRole`,
  `iam:PutRolePolicy`, `iot:CreateTopicRule`, etc.) — broader than what the
  *created* IoT role itself is granted, since deploying infrastructure
  naturally needs more privilege than the infrastructure needs at runtime.
- No IoT Thing, certificate, or device policy is required for this
  template specifically — it only wires the topic rule to DynamoDB. Those
  matter for how devices publish to `sentinelgrid/telemetry`, which was
  already set up and tested in the AWS IoT publishing step.

**This template and the `sentinelgrid_telemetry_to_dynamodb` rule it
creates are unaffected by the Lambda infrastructure below** — nothing in
`anomaly-detector.yaml` modifies this file, this rule, or the DynamoDB
table.

## anomaly-detector.yaml (current)

Defines the Lambda function that will eventually wrap the already-tested,
AWS-independent anomaly detector
([lambda_functions/anomaly_detector/](../lambda_functions/anomaly_detector/)).
**No IoT Rule invokes this function yet** — that wiring (a Lambda action
on a *new* IoT Rule, separate from `sentinelgrid_telemetry_to_dynamodb`)
is a deliberately separate, later step, done only after this Lambda is
deployed and verified independently.

```
AWS IoT Core
      |
      +----------------------> DynamoDB   (existing, unchanged)
      |
      +----------------------> Lambda     (this template — not yet wired up)
                                  |
                                  v
                         anomaly_detector.detect_telemetry()
                                  |
                                  v
                         DetectionResult
```

### Resources created

| Resource | Type | Purpose |
|---|---|---|
| `AnomalyDetectorFunction` | `AWS::Lambda::Function` (`SentinelGridAnomalyDetector`) | Runs [`lambda_functions.anomaly_detector.handler.lambda_handler`](../lambda_functions/anomaly_detector/handler.py). Runtime `python3.12` — a currently supported AWS Lambda runtime, chosen deliberately independent of this repo's local `.python-version` (3.9): Lambda's `python3.9` runtime is deprecated, and 3.12 gives substantially more remaining support lifetime than 3.10/3.11. The detector/handler use only standard-library features, so no code changes were needed for compatibility. `arm64` architecture (matches the dev environment and is cheaper — the handler is pure Python with no compiled dependencies, so architecture choice doesn't affect correctness). 128 MB memory, 10s timeout — generous for a deterministic, allocation-light function with no I/O. No VPC config (nothing here needs private network access). |
| `AnomalyDetectorExecutionRole` | `AWS::IAM::Role` (`SentinelGridAnomalyDetectorExecutionRole`) | Trusted only by `lambda.amazonaws.com`. One inline policy grants exactly `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`, scoped to this function's own CloudWatch Logs log group ARN — nothing else. No DynamoDB, IoT, Step Functions, or SNS access; no managed policies; no `*` actions. |

### Deployment package

[`package-anomaly-detector.sh`](package-anomaly-detector.sh) builds the
zip Lambda needs: it copies only `lambda_functions/` and `shared/` source
files (`.py` only — no `README.md`, `examples/`, tests, or `venv/`).
The handler and detector currently import nothing beyond the Python
standard library, so this script does **not** run `pip install` — there
is nothing to bundle. If a future version of the detector gains a real
third-party dependency, this script's comments note exactly where a
`pip install --target` step would need to go.

```bash
./infrastructure/package-anomaly-detector.sh
# writes infrastructure/build/anomaly-detector.zip (git-ignored)
```

### Handler path

```
lambda_functions.anomaly_detector.handler.lambda_handler
```

This is the exact dotted path the `Handler` property points to, matching
the deployment package's internal layout (`lambda_functions/anomaly_detector/handler.py`).

### IAM permissions

Only what basic Lambda execution logging requires — nothing more:

- `logs:CreateLogGroup`
- `logs:CreateLogStream`
- `logs:PutLogEvents`

All three are scoped to
`arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/SentinelGridAnomalyDetector:*`
— not `Resource: "*"`. No DynamoDB access is granted at this stage (the
function doesn't need it yet, and adding it prematurely would violate
least-privilege); that will be added only when the detector's output
actually needs to be persisted or acted on.

### Deploying

**Update:** as of Step 9, this Lambda function and its execution role
have been deployed and independently verified in AWS (see
[lambda_functions/anomaly_detector/README.md](../lambda_functions/anomaly_detector/README.md)).
The instructions below remain accurate for redeploying/updating the
stack.

To deploy (or redeploy) it yourself:

```bash
DEPLOYMENT_BUCKET=my-existing-versioned-bucket ./infrastructure/deploy-anomaly-detector.sh
```

This script verifies the bucket exists and has versioning enabled,
packages, uploads the zip to S3, retrieves the resulting S3 object
version, then runs `aws cloudformation deploy` with that version pinned.
It embeds no credentials, uses the AWS CLI's normal profile resolution
(`AWS_PROFILE=sentinelgrid`, `AWS_REGION=ap-south-1` by default, both
overridable), and fails with a clear message if `DEPLOYMENT_BUCKET` isn't
set, doesn't exist, or isn't versioned.

#### Why an immutable S3 object version, not just bucket/key

`AWS::Lambda::Function`'s `Code` property (`S3Bucket`/`S3Key`) is just a
*location* — it names where to find the code, not what the code actually
is. If you overwrite the object at that same key with new content and
redeploy with the exact same `S3Bucket`/`S3Key` parameter values,
CloudFormation sees no property change on the resource and reports
**"No changes to deploy"**, even though the underlying object's bytes are
completely different. This was hit in practice: a new package (with
added structured logging) was uploaded to the same key, but the Lambda's
code was never updated because nothing in the stack's declared properties
changed.

The fix is `S3ObjectVersion`, populated from the new
`DeploymentPackageVersion` parameter (required, no default — see the
template). An S3 **VersionId** is generated fresh by S3 every time an
object is written, but **only if versioning is enabled on the bucket** —
without it, every upload just silently replaces the previous object with
no distinguishable identity at all. Pinning the Lambda to a specific
VersionId means every new upload produces a genuinely different property
value, so CloudFormation always detects the change correctly.

`infrastructure/deploy-anomaly-detector.sh` obtains that VersionId itself
— you never need to look it up or pass it manually:

1. `aws s3api head-bucket` — confirms the bucket exists and is
   accessible.
2. `aws s3api get-bucket-versioning --query Status` — confirms versioning
   is `Enabled`. If it's `Suspended`, or the bucket was never configured
   for versioning at all, the script **fails with a clear error and
   exits** — it does not silently fall back to deploying an unversioned
   (and therefore update-unsafe) object.
3. `aws s3 cp` — uploads the package, as before.
4. `aws s3api head-object --query VersionId` — reads back the VersionId
   S3 assigned to the object just uploaded.
5. That VersionId is passed as `DeploymentPackageVersion` to
   `aws cloudformation deploy`, so the stack update always points Lambda
   at the exact immutable object version from step 3–4, never just "the
   current content of this key" (which could keep changing later).

### Before you deploy (manual prerequisites)

- **An S3 bucket you already control, with versioning enabled**, passed
  as `DEPLOYMENT_BUCKET`. **This project does not create one, and does
  not enable versioning on it, for you** — the deploy script verifies
  both and fails clearly rather than silently proceeding. Create/enable
  it yourself, e.g.:
  ```bash
  aws s3 mb s3://your-bucket-name --profile sentinelgrid --region ap-south-1
  aws s3api put-bucket-versioning --bucket your-bucket-name \
    --versioning-configuration Status=Enabled \
    --profile sentinelgrid --region ap-south-1
  ```
- The `sentinelgrid` AWS CLI profile authenticated, as in the DynamoDB
  step, with permission to create Lambda functions and IAM roles
  (`lambda:CreateFunction`, `iam:CreateRole`, `iam:PutRolePolicy`, plus S3
  read/write access to your chosen bucket, including
  `s3:GetBucketVersioning` and `s3:GetObjectVersion`).
- Nothing else — this template does not touch IoT, DynamoDB, or the
  existing `sentinelgrid_telemetry_to_dynamodb` rule in any way.

## anomaly-detection-rule.yaml (deployed — direct path DISABLED as of Step 26)

**(Step 26 update)**: the `sentinelgrid_telemetry_to_lambda` rule below
is now deployed with `RuleDisabled: true` — the direct IoT -> Analyzer
path it describes has been superseded by
`sentinelgrid_telemetry_to_sqs -> SentinelGridSqsTelemetryConsumer ->
Analyzer` (see "sqs-consumer.yaml" below), to avoid the same telemetry
message reaching the Analyzer twice. The rule resource, its Lambda
action, and its invoke permission are deliberately **preserved, not
deleted**, for historical traceability — everything below this note
describes what the rule does when enabled, which was its state through
Step 25.

Adds the second branch of telemetry fan-out: a new, independent IoT Rule
that invokes the already-deployed `SentinelGridAnomalyDetector` Lambda
for every message on `sentinelgrid/telemetry`, alongside (not replacing)
the existing DynamoDB rule:

```
AWS IoT Core
      |
      +----------------------> DynamoDB   (sentinelgrid_telemetry_to_dynamodb — existing, untouched)
      |
      +----------------------> Lambda     (sentinelgrid_telemetry_to_lambda — this template)
                                  |
                                  v
                         SentinelGridAnomalyDetector
                                  |
                                  v
                         anomaly_detector.detect_telemetry()
                                  |
                                  v
                         DetectionResult
```

**Deployed and verified.** Real invocations via this rule return
`detected=false`/`NORMAL` for normal telemetry and
`detected=true`/`NODE_OVERLOAD`/`MEDIUM` for overloaded telemetry, and the
Lambda's structured detection-result log line is confirmed in CloudWatch.

### What it does

- Consumes the **same** `sentinelgrid/telemetry` MQTT topic the DynamoDB
  rule already consumes — both rules receive every message independently;
  neither affects the other's delivery.
- Uses the same `SELECT * FROM 'sentinelgrid/telemetry'` SQL and
  `AwsIotSqlVersion: "2016-03-23"` as the DynamoDB rule, for consistency.
- Invokes `SentinelGridAnomalyDetector` (see
  [anomaly-detector.yaml](anomaly-detector.yaml) above) via a **Lambda**
  action, passing the full telemetry payload as the Lambda event.
- The Lambda runs [`lambda_functions.anomaly_detector.handler.lambda_handler`](../lambda_functions/anomaly_detector/handler.py),
  which delegates to `detect_telemetry()` and **returns a
  `DetectionResult`** — this template does not persist, alert on, or act
  on that result in any way (see below).
- Is intended to become the **detection stage** of SentinelGrid's
  eventual self-healing pipeline: once wired up, this is the point where
  raw telemetry starts being classified as normal or anomalous in near
  real time, ahead of any future remediation logic.

### Resources created

| Resource | Type | Purpose |
|---|---|---|
| `AnomalyDetectorInvokePermission` | `AWS::Lambda::Permission` | Lets **only** `iot.amazonaws.com` invoke `SentinelGridAnomalyDetector`, and only via **this specific rule** (`SourceArn` scoped to the `sentinelgrid_telemetry_to_lambda` rule ARN) — not a public or unscoped permission. |
| `AnomalyDetectionTopicRule` | `AWS::IoT::TopicRule` (`sentinelgrid_telemetry_to_lambda`) | Subscribes to `sentinelgrid/telemetry` and invokes the Lambda above. `DependsOn` the permission, so the permission is guaranteed to exist before the rule that relies on it. |

**No new IAM role is created.** Unlike the DynamoDB rule's `DynamoDBv2`
action (which needs an IoT-assumed role to call `PutItem`), an IoT Rule's
**Lambda** action authorizes purely through the Lambda resource-based
permission above — there is no `RoleArn` on this action type, and no
service role for IoT to assume. The existing
`SentinelGridAnomalyDetectorExecutionRole` (CloudWatch Logs only) is
**not modified or extended** — IoT invoking the function doesn't require
the function's own execution role to gain anything new.

### What this step deliberately does NOT create

- No detection-history persistence of `DetectionResult` — see
  `anomaly-events-storage.yaml` below for that (implemented locally, not
  yet deployed).
- No CloudWatch alarms, SNS topics, or notifications.
- No Step Functions or remediation logic.
- No changes whatsoever to `telemetry-storage.yaml`,
  `sentinelgrid_telemetry_to_dynamodb`, or `anomaly-detector.yaml`.

### Deploying

```bash
./infrastructure/deploy-anomaly-detection-rule.sh
```

Requires no parameters by default (the Lambda function name defaults to
`SentinelGridAnomalyDetector`, overridable via
`ANOMALY_DETECTOR_FUNCTION_NAME` if needed). Uses
`AWS_PROFILE=sentinelgrid` and `AWS_REGION=ap-south-1` by default, embeds
no credentials, and never touches the DynamoDB stack.

### Before you deploy (manual prerequisites)

- `SentinelGridAnomalyDetector` must already be deployed (it is, as of
  Step 9) — this template references it by name; it does not create it.
- The `sentinelgrid` profile needs `iot:CreateTopicRule` and
  `lambda:AddPermission` in addition to what earlier steps required.
- Nothing else — no S3 bucket, no packaging step (this stack contains no
  Lambda code of its own).

## anomaly-events-storage.yaml (current — implemented locally, NOT deployed)

Adds persistent, auditable storage for anomaly detection results —
completely separate from `SentinelGridTelemetry` above:

```
AWS IoT -> SentinelGridAnomalyDetector -> DetectionResult
    -> AnomalyEventStore.put_event()   (lambda_functions/anomaly_detector/event_store.py)
    -> SentinelGridAnomalyEvents        (this template — not yet deployed)
```

**Not yet deployed.** Writing and testing this template creates nothing
in AWS — the table does not exist yet, and the anomaly detector Lambda's
execution role does not yet have permission to write to it (see below).

### Resources created

| Resource | Type | Purpose |
|---|---|---|
| `AnomalyEventsTable` | `AWS::DynamoDB::Table` (`SentinelGridAnomalyEvents`) | Stores one item per detected anomaly. `PAY_PER_REQUEST` billing, no provisioned capacity. |

That's the **only** resource this template creates — no IAM role, no IoT
Rule, no GSIs. It is the simplest of the three DynamoDB-adjacent
templates in this directory.

**Step 35 addition**: the table's ARN was already exported
(`SentinelGridAnomalyEventsTableArn`) from the start; the table's *name*
is now also exported (`SentinelGridAnomalyEventsTableName`) — a purely
additive `Outputs` change (the `AnomalyEventsTable` resource itself is
unchanged) — so the new `sentinelgrid-knowledge-query` stack (see
"Knowledge Base Query Interface (Step 35)" below) can populate its Query
Lambda's `ANOMALY_EVENTS_TABLE_NAME` environment variable via
`!ImportValue` instead of a hardcoded literal.

### Table key design

- **Partition key**: `node_id` (String)
- **Sort key**: `timestamp` (String)

Deliberately identical in shape to `SentinelGridTelemetry`'s own key
design (see above) — every anomaly for a node can be queried
chronologically, the same way every telemetry reading for a node can.

### Why a separate table, not a section of SentinelGridTelemetry

Telemetry and anomaly events have different volumes (every reading vs.
only anomalous ones), different consumers (the future dashboard/analyzer
vs. an audit trail), and different lifecycles. Mixing them into one table
would mean every query needs to filter one out from the other; two
purpose-built tables with the same key shape avoids that for a
negligible cost (DynamoDB `PAY_PER_REQUEST` billing scales with actual
usage, not with the number of tables).

### Application-level persistence

[`lambda_functions/anomaly_detector/event_store.py`](../lambda_functions/anomaly_detector/event_store.py)
is the only code that writes to this table — `boto3` lives there and
nowhere else in the `anomaly_detector` package;
`detector.py` remains completely AWS-independent. See
[lambda_functions/anomaly_detector/README.md](../lambda_functions/anomaly_detector/README.md)'s
"Anomaly event persistence" section for the full write path, when an
event is/isn't written, failure behavior (fail-fast — a write failure
propagates and fails the Lambda invocation, rather than silently
pretending to succeed), and the idempotent conditional-write design.

### Deploying (not yet done)

```bash
./infrastructure/deploy-anomaly-events-storage.sh
```

No parameters required. Uses `AWS_PROFILE=sentinelgrid` and
`AWS_REGION=ap-south-1` by default (both overridable), embeds no
credentials, and creates no IAM resources — so no `--capabilities` flag
is needed, unlike `telemetry-storage.yaml`'s deploy script.

### Before you deploy (manual prerequisites)

- The `sentinelgrid` profile needs `dynamodb:CreateTable` in addition to
  what earlier steps required.
- **This alone is not enough to make persistence work end-to-end.** The
  anomaly detector Lambda's execution role
  (`SentinelGridAnomalyDetectorExecutionRole`, defined in
  `anomaly-detector.yaml`) does not yet have `dynamodb:PutItem` on this
  table — granting that is a deliberately separate, later step (adding
  IAM permissions was explicitly out of scope for this one). Until both
  the table exists *and* the role can write to it, a live anomaly
  detection would hit `AnomalyEventPersistenceError` and fail the
  invocation — which is the documented, intended fail-fast behavior, not
  a bug to work around by deploying this out of order.

## telemetry-kinesis-stream.yaml (Step 24 — local design complete, AWS deployment BLOCKED)

Adds the Kinesis ingestion/buffering layer required by SRS FR-3.1/3.2/3.3:

```
AWS IoT Core (sentinelgrid/telemetry, existing topic)
      |
      +----------------------> DynamoDB   (sentinelgrid_telemetry_to_dynamodb — existing, unchanged)
      |
      +----------------------> Lambda     (sentinelgrid_telemetry_to_lambda — existing, unchanged)
      |
      +----------------------> Kinesis    (sentinelgrid_telemetry_to_kinesis — this template)
                                  |
                                  v
                         SentinelGridTelemetryStream
                         (no consumer yet — a later step)
```

### Resources defined

| Resource | Type | Purpose |
|---|---|---|
| `TelemetryStream` | `AWS::Kinesis::Stream` (`SentinelGridTelemetryStream`) | `ON_DEMAND` capacity mode (no shard count to manage/pay for while idle), AWS-managed KMS encryption (`alias/aws/kinesis`), `RetentionPeriodHours` parameterized (default 24 — FR-3.2). `DeletionPolicy`/`UpdateReplacePolicy: Retain`. |
| `IoTToKinesisRole` | `AWS::IAM::Role` (`SentinelGridIoTToKinesisRole`) | Trusts only `iot.amazonaws.com`; one inline policy grants exactly `kinesis:PutRecord`, scoped to the stream's own ARN — no wildcard, no other action. |
| `TelemetryToKinesisRule` | `AWS::IoT::TopicRule` (`sentinelgrid_telemetry_to_kinesis`) | Third, independent rule on the same `sentinelgrid/telemetry` topic (`SELECT *`, full payload, no schema transformation). Kinesis action partitions by `${subsystem}` (FR-3.3) — not `node_id`. |

Purely additive: does not modify `telemetry-storage.yaml`, `anomaly-detector.yaml`, `anomaly-detection-rule.yaml`, or any remediation stack. The two existing IoT rules keep running unchanged, in parallel with this new one.

### Deployment status: BLOCKED at the AWS account level

Deployment was attempted (via a full change-set preview + execute, exactly as this project's established safety process requires) and **failed** — not due to a template or design defect:

```
Resource handler returned message: "Internal error occurred."
(HandlerErrorCode: InternalFailure)   — for TelemetryStream (AWS::Kinesis::Stream)
```

Corroborated directly by a raw `aws kinesis list-streams`/`describe-limits` call against this account (`401429382000`), in both `ap-south-1` and `us-east-1`:

```
SubscriptionRequiredException: The AWS Access Key Id needs a subscription for the service
```

This means **this AWS account has not been subscribed/enabled for Amazon Kinesis at all** — an account-level restriction, not an IAM permissions issue (the calls were made as the account root user) and not fixable from within this template or deploy script. The failed stack rolled back cleanly; no Kinesis stream, IAM role, or IoT rule was left behind (verified individually). This blocker must be resolved by the AWS account owner (typically via the AWS Console's service/billing activation) before this stack can be deployed.

The template, deploy script, and its local structural tests (`tests/test_telemetry_kinesis_stream_infrastructure.py`) are otherwise complete and passing — deployment is the only remaining step once the account's Kinesis access is enabled.

### Deploying (once the account-level blocker above is resolved)

```bash
./infrastructure/deploy-telemetry-kinesis-stream.sh
```

Uses `AWS_PROFILE=sentinelgrid` and `AWS_REGION=ap-south-1` by default, `RETENTION_PERIOD_HOURS=24` by default, embeds no credentials.

### Cost/safety notes

- `ON_DEMAND` capacity mode: no idle shard cost, scales automatically, billed per-GB ingested/retrieved — appropriate for a student project with no steady-state production load.
- 24-hour retention (the Kinesis minimum/standard tier) incurs no extra retention cost; retention beyond 24h uses Kinesis's paid "long-term retention" tier.
- AWS-managed KMS key (`alias/aws/kinesis`) — no additional KMS key cost, satisfies NFR-5 without introducing dedicated customer-managed key infrastructure.
- No consumer (Lambda, KCL application, etc.) is created by this step, so no additional compute cost is introduced even once deployed.

## telemetry-sqs-queue.yaml (Step 25 — deployed and verified)

Replaces the Kinesis ingestion layer attempted in Step 24 — not because
the Kinesis design was wrong, but because this AWS account cannot use
Kinesis at all (see the "telemetry-kinesis-stream.yaml" section above:
`SubscriptionRequiredException`, confirmed both via CloudFormation and
raw `aws kinesis` calls). This is an intentional, cost/account-plan-driven
architecture decision — the Kinesis template, deploy script, and tests
are preserved unmodified as historical record of that attempt and are
not deleted. The SRS itself will be updated to reflect SQS in a later
step; this template does not touch it.

```
AWS IoT Core (sentinelgrid/telemetry, existing topic)
      |
      +----------------------> DynamoDB   (sentinelgrid_telemetry_to_dynamodb — enabled, unchanged)
      |
      +----------------------> Lambda     (sentinelgrid_telemetry_to_lambda — DISABLED as of Step 26; see below)
      |
      +----------------------> SQS        (sentinelgrid_telemetry_to_sqs — this template, enabled)
                                  |
                                  v
                         SentinelGridTelemetryQueue  --(after 5 failed receives)--> SentinelGridTelemetryDLQ
                                  |
                                  v (Step 26)
                         SentinelGridSqsTelemetryConsumer -> Analyzer (SentinelGridAnomalyDetector)
```

**(Step 26 update)** `VisibilityTimeoutSeconds` was raised from this
step's original default of 30 to **90** — see the "sqs-consumer.yaml"
section below for why (AWS Lambda's SQS event source mapping requires
`VisibilityTimeout >= ` the consuming Lambda's own `Timeout`, confirmed
live when 30 was rejected by `CreateEventSourceMapping`).

**Deployed and live-verified.** Stack `sentinelgrid-telemetry-sqs-queue`, `CREATE_COMPLETE`.

### Resources created

| Resource | Type | Purpose |
|---|---|---|
| `TelemetryQueue` | `AWS::SQS::Queue` (`SentinelGridTelemetryQueue`) | Standard (not FIFO — this project has no strict-ordering requirement). `MessageRetentionPeriod` parameterized (default 345600s / 4 days — SQS's own default). `VisibilityTimeout` parameterized (default 30s — no consumer exists yet, so this is a placeholder for a future consumer step to tune). `SqsManagedSseEnabled: true` (AWS-managed SSE-SQS encryption, satisfies NFR-5 with zero extra KMS infrastructure/cost). `RedrivePolicy` to `TelemetryDLQ` after 5 failed receive attempts. `DeletionPolicy`/`UpdateReplacePolicy: Retain`. |
| `TelemetryDLQ` | `AWS::SQS::Queue` (`SentinelGridTelemetryDLQ`) | Holds messages that failed processing 5 times. Deliberately longer retention (1209600s / 14 days, the SQS maximum) than the main queue, since anything here already represents a processing failure worth the widest possible investigation window. Same AWS-managed encryption. `Retain` policy. |
| `IoTToSqsRole` | `AWS::IAM::Role` (`SentinelGridIoTToSqsRole`) | Trusts only `iot.amazonaws.com`; one inline policy grants exactly `sqs:SendMessage`, scoped to `SentinelGridTelemetryQueue`'s own ARN — no `sqs:*`, no wildcard resource, no other service permission. |
| `TelemetryToSqsRule` | `AWS::IoT::TopicRule` (`sentinelgrid_telemetry_to_sqs`) | Third, independent rule on the existing `sentinelgrid/telemetry` topic (`SELECT *`, `UseBase64: false` — the exact telemetry JSON becomes the message body, unwrapped, unmodified). |

### Partitioning: SQS has none — subsystem stays in the message instead

Kinesis's shard-partition-key concept doesn't exist for SQS Standard queues, and this step does not try to fake it. Instead, the complete telemetry payload — including its existing `subsystem` field (`"power"`/`"water"`/`"traffic"`) — flows into the queue unchanged. A future consumer can filter/route by reading that field from each message body; one shared queue serves all three subsystems, exactly as `SELECT *` and `UseBase64: false` already guarantee no field is dropped or transformed.

### What this step does NOT do yet

- No live telemetry was published into the queue by this step — `ApproximateNumberOfMessages: 0` confirmed live after deployment.
- Does not modify `telemetry-storage.yaml` or any remediation stack.

**(Step 26 update)**: a consumer now exists — see "sqs-consumer.yaml"
below — and the direct `sentinelgrid_telemetry_to_lambda` IoT rule has
since been disabled (not deleted) as part of that step's cutover.

### Deploying

```bash
./infrastructure/deploy-telemetry-sqs-queue.sh
```

Uses `AWS_PROFILE=sentinelgrid` and `AWS_REGION=ap-south-1` by default, `MESSAGE_RETENTION_PERIOD_SECONDS=345600`/`VISIBILITY_TIMEOUT_SECONDS=90` (raised from 30 in Step 26)/`MAX_RECEIVE_COUNT=5` by default, embeds no credentials, does not retry a failed deployment.

### Cost/safety notes

- Both queues: no idle cost (SQS has no per-hour charge, only per-request/per-GB), so an unused queue costs nothing beyond negligible storage of buffered messages.
- AWS-managed SSE-SQS encryption: no additional KMS key cost.
- No compute (Lambda, EC2, etc.) created by this step — nothing runs continuously, so no always-on cost exists yet.
- At SentinelGrid's demonstration telemetry volume (dozens of messages, not a sustained production stream), this comfortably fits within the AWS Free Tier's SQS allowance (1M requests/month) regardless of the account's Kinesis restriction.

## sqs-consumer.yaml (Step 26 — deployed and verified, cutover complete)

Adds the missing consumer between Step 25's SQS buffer and the existing
Analyzer, and performs the resulting cutover away from the direct IoT ->
Analyzer path:

```
AWS IoT Core (sentinelgrid/telemetry)
      |
      +----------------------> DynamoDB   (sentinelgrid_telemetry_to_dynamodb — enabled, unchanged)
      |
      +----------------------> Lambda     (sentinelgrid_telemetry_to_lambda — DISABLED, preserved)
      |
      +----------------------> SQS        (sentinelgrid_telemetry_to_sqs — enabled)
                                  |
                                  v
                         SentinelGridTelemetryQueue
                                  |
                                  v  (AWS::Lambda::EventSourceMapping, BatchSize=5, ReportBatchItemFailures)
                         SentinelGridSqsTelemetryConsumer
                                  |
                                  v  (lambda:InvokeFunction, synchronous)
                         SentinelGridAnomalyDetector (unmodified)
                                  |
                                  v
                         detect_telemetry() -> persist -> remediation trigger -> Step Functions
                         (unchanged pipeline, all proven live in Step 23)
```

**Deployed and live-verified.** Stack `sentinelgrid-sqs-consumer`,
`CREATE_COMPLETE`. No telemetry/data-plane test was performed as part of
this step — the queue's `ApproximateNumberOfMessages` was confirmed `0`
both before and after deployment.

### Why a consumer Lambda, not a direct SQS-to-Lambda-console trigger, and why it invokes rather than embeds the Analyzer

See [`lambda_functions/sqs_consumer/README.md`](../lambda_functions/sqs_consumer/README.md)
for the full reasoning. In short: the consumer invokes the already-deployed
`SentinelGridAnomalyDetector` via a **synchronous** (`RequestResponse`)
`lambda:InvokeFunction` call, rather than embedding a second copy of the
Analyzer's detection/persistence/trigger-submission code — this keeps
exactly one deployed Analyzer implementation, and (as a direct
consequence) means the consumer's own deployment package never needs
anything from `lambda_functions/anomaly_detector/`, sidestepping that
package's eager-import ripple effect entirely (the same ripple that once
required fixes across three other packaging scripts in Step 22).

### Resources created

| Resource | Type | Purpose |
|---|---|---|
| `SqsConsumerExecutionRole` | `AWS::IAM::Role` (`SentinelGridSqsConsumerExecutionRole`) | Trusts only `lambda.amazonaws.com`. Three inline policies: CloudWatch Logs (own log group only); `lambda:InvokeFunction` scoped to exactly `SentinelGridAnomalyDetector`'s ARN; `sqs:ReceiveMessage`/`sqs:DeleteMessage`/`sqs:GetQueueAttributes` scoped to exactly `SentinelGridTelemetryQueue`'s ARN — the minimum AWS documents as required for an event source mapping to poll SQS on the function's behalf (no `sqs:SendMessage`, no `sqs:*`, no wildcard resource anywhere). |
| `SqsConsumerFunction` | `AWS::Lambda::Function` (`SentinelGridSqsTelemetryConsumer`) | Python 3.12, arm64, 128MB, 60s timeout (sized for a worst-case batch of 5 sequential synchronous Analyzer invokes at the Analyzer's own 10s ceiling). `ANOMALY_DETECTOR_FUNCTION_ARN` environment variable. |
| `SqsEventSourceMapping` | `AWS::Lambda::EventSourceMapping` | Source: `SentinelGridTelemetryQueue`. Target: the function above. `BatchSize=5` (parameterized), `FunctionResponseTypes: [ReportBatchItemFailures]` — so a bad message in a batch is reported individually via `{"batchItemFailures": [...]}` rather than causing the whole batch (including already-succeeded messages) to be redelivered. |

### Batch size and failure handling

- `BatchSize=5`, not SQS's own default of 10: each message triggers a
  **synchronous** invoke of the Analyzer, whose own Lambda `Timeout` is
  10s — a batch of 5 processed sequentially bounds worst-case total
  processing at 50s, under this function's own 60s `Timeout`. A batch of
  10 would allow a 100s worst case, exceeding it.
- Per-record failure classification (invalid JSON body, non-object JSON,
  or an Analyzer invocation/`FunctionError` failure) is reported via
  `batchItemFailures` — never silently discarded, never causing the rest
  of the batch to abort. See
  [`lambda_functions/sqs_consumer/handler.py`](../lambda_functions/sqs_consumer/handler.py).
- The consumer never calls `sqs:DeleteMessage` itself in application
  code — despite the role granting it (AWS requires it for the event
  source mapping's own polling) — message acknowledgement is entirely
  the event source mapping's responsibility, driven by the returned
  `batchItemFailures`.

### An AWS-enforced constraint this step surfaced: queue VisibilityTimeout >= consumer Timeout

The first deployment attempt of `SqsEventSourceMapping` failed —
`CreateEventSourceMapping` rejected it outright:
`"Queue visibility timeout: 30 seconds is less than Function timeout: 60
seconds"`. This is a hard AWS requirement, not a tunable warning. Fixed
by raising `SentinelGridTelemetryQueue`'s `VisibilityTimeoutSeconds` from
30 to **90** (see "telemetry-sqs-queue.yaml" above) via a previewed,
Modify-only change set on the existing Step 25 stack — no replacement,
no other resource touched. The stack's one failed `CREATE_FAILED`
attempt rolled back cleanly (confirmed no orphaned Lambda/IAM resources)
before this fix and the successful redeployment.

### Cutover: sentinelgrid_telemetry_to_lambda disabled

Performed only after the consumer stack above was deployed and verified
live (Lambda `LastUpdateStatus: Successful`, event source mapping
`State: Enabled`, IAM scoped as documented). A single, previewed
Modify-only change set (no replacement) flipped
`AnomalyDetectionTopicRule`'s `RuleDisabled` from `false` to `true` in
the existing `sentinelgrid-anomaly-detection-rule` stack — the rule
resource, its Lambda action, and its invoke permission are preserved in
the template and live, not deleted, for historical traceability. Live
verification after the cutover:

| Rule | State |
|---|---|
| `sentinelgrid_telemetry_to_lambda` | **disabled** |
| `sentinelgrid_telemetry_to_sqs` | enabled |
| `sentinelgrid_telemetry_to_dynamodb` | enabled, untouched |

Exactly one production route now exists from IoT telemetry to the
Analyzer: via SQS and this consumer.

### What this step does NOT do

- No telemetry was published, no simulator run, no consumer/Analyzer
  Lambda invoked directly, no Step Functions execution started — the
  controlled live test of this new path is a later step.
- Does not modify the Analyzer's own code, role, or configuration —
  `SentinelGridAnomalyDetector`'s `LastModified`/`CodeSha256` confirmed
  unchanged after this step.
- Does not modify the Remediation Trigger, Step Functions workflow,
  Planner, Executor, or Verifier — all confirmed unchanged
  (`LastUpdatedTime` predating this deployment).

### Deploying

```bash
DEPLOYMENT_BUCKET=sentinelgrid-lambda-deploy-401429382000 ./infrastructure/deploy-sqs-consumer.sh
```

Reads the Analyzer's `FunctionArn` and the queue's `QueueArn` live from
their existing stacks' Outputs (never hardcoded). Does not deploy the
Analyzer or queue stacks, and does not disable the direct IoT rule
itself — that cutover was performed as a separate, deliberate change set
against the existing `sentinelgrid-anomaly-detection-rule` stack.

## CloudWatch metrics (Step 28 — SRS FR-7.1's observability foundation)

Establishes the raw metric contract and publication only. **Not** the
CloudWatch dashboard (FR-7.2), alarms, or SNS notifications (FR-7.3) —
those are later steps. See `shared/metrics.py`'s module docstring and
`lambda_functions/sqs_consumer/README.md`-style reasoning docstrings in
each instrumented handler for the full design detail; this section
summarizes it.

**CloudWatch metric publication is best-effort and does not participate
in the correctness path.** `shared/metrics.py`'s `MetricsClient` catches
every failure from `cloudwatch:PutMetricData`, logs it (metric name,
subsystem, fault type, error type — never credentials or full telemetry
payloads), and returns `False` — it never raises. This is the one
deliberate exception to this project's usual AWS-adapter fail-fast
convention (`event_store.py`/`remediation_trigger_client.py` both fail
loudly, because a lost write there would silently break the self-healing
pipeline itself); a CloudWatch outage must never fail a successful
detection, block persistence, or alter Step Functions semantics.

### Namespace

`SentinelGrid` — one project-wide namespace, not scattered per-Lambda.

### Dimensions

`Subsystem` and `FaultType` only. **Never `node_id`**: the system may
support hundreds of virtual nodes, and a per-node dimension would create
a new CloudWatch metric stream per node — inappropriate cardinality for
this project's low-cost account. `MetricsClient` skips publication
entirely (logging why) rather than publish with a missing/empty
dimension value.

### Metric table

| Metric | Unit | Dimensions | Owner | Purpose |
|---|---|---|---|---|
| `FaultDetectionCount` | Count | Subsystem, FaultType | Analyzer (`anomaly_detector/handler.py`) | Incremented once per invocation where `detected=True` and a usable subsystem/fault-type identity exists — regardless of what persistence/trigger-submission do afterward. |
| `DetectionLatency` | Milliseconds | Subsystem, FaultType | Analyzer (`anomaly_detector/handler.py`) | `detection time - telemetry timestamp`. Published only when the telemetry timestamp parses and is not in the future (see "Latency calculation" below). |
| `RecoveryAttemptCount` | Count | Subsystem, FaultType | Executor (`remediation/executor.py`) | Published once a valid plan is extracted with `remediation_required=True`, `automatic=True`, `escalation_required=False` — i.e. an actual automatic remediation attempt, not merely a detected anomaly. |
| `RecoverySuccessCount` | Count | Subsystem, FaultType | Executor | Published when `RemediationExecutor.execute()` returns `ExecutionStatus.SUCCESS`. |
| `RecoveryFailureCount` | Count | Subsystem, FaultType | Executor | Published for `ExecutionStatus.REJECTED` (an unsupported action — defensive, not currently reachable from a real `RemediationEngine.plan()` output) and `ExecutionStatus.ESCALATED` (also defensive here — see below). |
| `RecoveryLatency` | Milliseconds | Subsystem, FaultType | Executor | `remediation completion time - detection_result.timestamp`, using the detection timestamp already present in the workflow's accumulated input (Step 28 does not add any new ASL field). Published only on `SUCCESS`. |
| `UnresolvedFaultCount` | Count | Subsystem, FaultType | **Two owners** — Planner (`remediation/planner.py`) and Verifier (`remediation/verifier.py`) — see "UnresolvedFaultCount ownership" below | A fault that requires escalation/manual intervention and is therefore unresolved. |

### UnresolvedFaultCount ownership (why two owners, and why that is not double-counting)

`step_functions/remediation_workflow.json`'s `Escalate` state (a `Pass`
state, not a Task — no Lambda runs there) is reached from **four**
places: `CheckAutomaticRemediation` (automatic == false),
`ExecuteRemediation`'s `Catch`, `CheckVerification` (verified == false),
and `VerifyRemediation`'s `Catch`. Step 28 does not modify the ASL, so it
cannot add a fifth, single "escalation Lambda" — instead, `UnresolvedFaultCount`
is published from the two places that decide escalation as part of their
own existing business logic:

- **Planner** — when `RemediationEngine.plan()` decides
  `escalation_required=True` for a fault that needs remediation
  (`remediation_required=True`, `automatic=False`). This covers the
  majority of this project's escalation cases (HIGH/CRITICAL
  `NODE_OVERLOAD`, `INVALID_INPUT`, an unknown fault type) and is the
  **only** place such an event's escalation can be observed at all,
  since `CheckAutomaticRemediation` routes straight to `Escalate`
  without ever invoking the Executor.
- **Verifier** — when a remediation the Executor reported as `SUCCESS`
  does NOT actually verify (`verified=False`). `CheckVerification`
  routes this straight to `Escalate` too.

These two cases are mutually exclusive for any single event: a plan the
Planner marks `escalation_required=True` never reaches the Executor (so
the Verifier is never invoked for it either), and the Verifier only ever
sees a plan the Planner already marked `automatic=True` (so the Planner
never published `UnresolvedFaultCount` for it). `Executor.py` also has
its own defensive `ExecutionStatus.ESCALATED` branch (for direct/local
invocation with a hand-crafted plan bypassing the real Planner) that
also publishes `UnresolvedFaultCount` — this is provably unreachable via
the real, ASL-driven production path (`RemediationEngine.plan()` never
produces `automatic=True` together with `escalation_required=True`), so
it can never double-count against the Planner's or Verifier's own
publication in practice.

**Known limitation**: the two `Catch`-block escalation paths (a genuine
unhandled exception inside the Executor or Verifier Lambda itself) are
NOT instrumented — that would require either modifying the ASL
(forbidden) or wrapping these handlers in broad try/except/re-raise
logic around a failure mode this project's Lambdas don't currently
produce in practice (both are deterministic, pure-Python simulations
with no I/O that can fail). This is a real, documented gap, not an
invented "true active/unresolved gauge" — `UnresolvedFaultCount` does
not currently decrement, either: it has no lifecycle point yet at which
an unresolved fault becomes resolved, so it is a raw incrementing
observation, not an active-fault gauge (see FR-7.2/FR-7.3 for future
dashboard/alarm work that would need such a gauge).

### Latency calculation

Both `DetectionLatency` and `RecoveryLatency` currently measure from the
**same underlying field** — the originating telemetry event's own
`timestamp`, never a CloudWatch ingestion time. `RecoveryLatency`
therefore actually measures "time to detect AND remediate" combined,
not isolated remediation time alone — there is no separately-tracked
"actual detection completed" wall-clock field in the current
`DetectionResult`/workflow-input contract, and Step 28 was explicitly
directed to use the detection timestamp already present in the
workflow input rather than add a new ASL field. `shared.metrics.
compute_elapsed_ms()` never raises: a missing, unparseable, or future
(clock-skew) timestamp is logged and causes that one latency
observation to be skipped — detection/persistence/remediation continue
normally either way.

**Step 28 provides raw recovery-latency observations, not a computed
MTTR.** A later dashboard/aggregation step can derive mean recovery
latency ("MTTR-style"), detection latency, recovery success rate, active
faults, and subsystem health from these raw CloudWatch data points —

**Step 36 update**: the above was independently re-verified against a
controlled 10-event live test, decomposed into distinct
telemetry-to-detection / detection-processing / detection-to-remediation-
start / remediation-workflow / telemetry-to-recovery phases using
existing AWS-native evidence (CloudWatch Logs `REPORT` lines, Step
Functions execution history) — no new metric, Lambda code, or ASL change
was needed. See `docs/performance-validation.md` for the full
methodology, results, the Step 29 ~15s-latency root-cause finding (a
cold-start/idle-pipeline effect, not a property of the detection or
remediation logic itself), and the honest NFR-1/NFR-2 assessment (neither
is yet demonstrated at a statistically meaningful sample size).
none of that statistical aggregation is invented or computed here.

### Detection accuracy / false-positive rate — not implemented in Step 28

The SRS's FR-7.1 also lists detection accuracy and false-positive rate.
**Step 28 does not publish either.** This project's production system
has no labeled ground-truth dataset (a record of which telemetry events
were actually, independently confirmed anomalous vs. normal) sufficient
to calculate a true accuracy or false-positive rate — publishing an
invented number (e.g. the SRS's own 95% target, restated as if
measured) would misrepresent this system's actual, unverified detection
quality. This gap is documented here rather than filled with a
fabricated value.

### IAM permissions

Exactly the Lambdas that publish metrics gained one new inline policy
each — `cloudwatch:PutMetricData`, scoped by the AWS-documented
`cloudwatch:namespace` condition key (`PutMetricData` has no
ARN-level resource scoping at all, so `Resource: "*"` is required by
the action itself; the condition is the real authorization boundary):

- `SentinelGridAnomalyDetectorExecutionRole` — `SentinelGridAnomalyDetectorPublishMetrics`
- `SentinelGridRemediationPlannerRole` — `SentinelGridRemediationPlannerPublishMetrics`
- `SentinelGridRemediationExecutorRole` — `SentinelGridRemediationExecutorPublishMetrics`
- `SentinelGridRemediationVerifierRole` — `SentinelGridRemediationVerifierPublishMetrics`

**Not granted** to `SentinelGridSqsConsumerExecutionRole` or
`SentinelGridRemediationTriggerExecutionRole` — neither Lambda publishes
metrics.

### Packaging

`shared/metrics.py` is now imported by
`lambda_functions/anomaly_detector/handler.py` (eagerly loaded via that
package's `__init__.py`) and by
`lambda_functions/remediation/{planner,executor,verifier}.py`. This
triggered the same packaging ripple-effect this project has hit before
(Step 22): `package-remediation-lambdas.sh` and
`package-remediation-trigger.sh` both bundle the Analyzer's `handler.py`
but did not previously bundle anything from `shared/` — the first Step
28 packaging attempt confirmed this the hard way (`ModuleNotFoundError:
No module named 'shared'` in an isolated-import test) before both
scripts were updated to also stage `shared/__init__.py` and
`shared/metrics.py`. `package-anomaly-detector.sh` already copied all of
`shared/*.py` and needed no change.

### Deployment status

Deployed via previewed, Modify-only change sets (no `Replacement`, no
unrelated resource touched) against the existing `sentinelgrid-anomaly-
detector` (2 resources: the function + its role) and
`sentinelgrid-remediation-lambdas` (6 resources: 3 functions + 3 roles)
stacks — both `UPDATE_COMPLETE`, all four Lambdas
`LastUpdateStatus: Successful`. No new stack was created for this step.

## CloudWatch Observability Dashboard (Step 30)

**Stack**: `sentinelgrid-observability-dashboard` — a dedicated stack
containing exactly one resource, `AWS::CloudWatch::Dashboard`. Purely a
read-only visualization layer over the metrics Step 28 already
publishes: creates no new metrics, no alarms, no SNS resources, no IAM.
Does not modify any other stack, Lambda, IoT rule, the SQS queue, or the
Step Functions state machine.

**Dashboard name**: `SentinelGrid-Observability` (deterministic, set via
`DashboardName` — not CloudFormation's auto-generated physical ID).

### Purpose and widget categories

A single-page operational view, laid out on CloudWatch's 24-column grid
(2 widgets per row where practical), covering:

- **System overview** (text) — the architectural chain (IoT → SQS →
  Analyzer → Remediation) and what each stage does, in prose only — no
  ARNs, account IDs, or credentials.
- **Fault detection** — `FaultDetectionCount` for the one combination
  with live data (`Subsystem=power`, `FaultType=NODE_OVERLOAD`, proven
  in Step 29), plus a second widget using a CloudWatch `SEARCH`
  expression (`{SentinelGrid,Subsystem,FaultType} MetricName=
  "FaultDetectionCount"`) that automatically plots one series per
  Subsystem/FaultType combination that has ever published data — no
  combination is hardcoded beyond the one already proven live.
- **Detection latency** — `DetectionLatency`, Average, 300s period,
  explicitly labeled in Milliseconds.
- **Recovery outcomes** — `RecoveryAttemptCount`/`RecoverySuccessCount`/
  `RecoveryFailureCount` together in one widget, Sum, 300s period.
- **Recovery latency** — `RecoveryLatency`, Average, 300s period, title
  explicitly states "raw observation — not MTTR" (see MTTR caveat
  below).
- **Unresolved faults** — `UnresolvedFaultCount`, Sum, 300s period.
- **Subsystem view** — two widgets using CloudWatch Metrics Insights
  (`SELECT SUM(FaultDetectionCount) FROM SCHEMA("SentinelGrid",
  Subsystem) GROUP BY Subsystem`, and the same for
  `RecoverySuccessCount`) — aggregates across whatever `FaultType`
  values exist per subsystem, without hardcoding `power`/`water`/
  `traffic` individually. A subsystem with no data yet simply shows no
  series — this is expected, not an error (see "Do not assume all
  subsystems have live data" in the Step 30 instructions).

All 7 existing metrics (`FaultDetectionCount`, `DetectionLatency`,
`RecoveryAttemptCount`, `RecoverySuccessCount`, `RecoveryFailureCount`,
`RecoveryLatency`, `UnresolvedFaultCount`) appear somewhere on the
dashboard. Default time range: `"start": "-PT6H"` (relative — always the
last 6 hours from whenever the dashboard is opened, never a hardcoded
historical timestamp).

### Dimension strategy

Every metric widget uses only `Subsystem` and `FaultType` — **never
`node_id`** — matching the Step 28 metric contract exactly. A standing
test (`test_node_id_never_used_as_a_dimension`) parses the dashboard's
actual `DashboardBody` JSON and inspects every widget's metric arrays
and search/Metrics-Insights expressions to enforce this structurally,
not just by string-matching the raw template text.

### Subsystem health — represented via existing signals, not fabricated

The SRS asks for subsystem health, but the current metric contract has
no `SystemHealth`/`SubsystemHealth`/`NodeHealth` metric — Step 30 does
**not** invent one. Subsystem health is represented instead through the
existing observable signals, viewed per subsystem: `FaultDetectionCount`
(how often faults occur), `RecoverySuccessCount`/`RecoveryFailureCount`
(how well the system recovers), `UnresolvedFaultCount` (what remains
unresolved), and `DetectionLatency`/`RecoveryLatency` (how quickly). A
later step could introduce a genuine derived health indicator computed
from these — Step 30 does not attempt that.

### MTTR caveat

`RecoveryLatency` is displayed as the raw metric it is — Average, over
300s periods — and is explicitly **not** labeled or presented as MTTR.
Its widget title states "raw observation — not MTTR," and a dashboard
text widget explains why: under the current Step 28 contract,
`RecoveryLatency = remediation completion time − detection_result.
timestamp`, which includes detection time, not isolated remediation
execution time. Computing an aggregated MTTR figure (e.g. a mean over a
time window) is left to a future reporting/analysis step.

### Detection accuracy / false-positive caveat

The dashboard displays no accuracy or false-positive-rate figure —
specifically, never the SRS's 95% target presented as if measured. A
text widget states plainly that detection accuracy and false-positive
rate require labeled ground-truth outcomes this project does not yet
have, and are not currently calculated.

### Deploying

```bash
./infrastructure/deploy-observability-dashboard.sh
```

Uses `AWS_PROFILE=sentinelgrid` and `AWS_REGION=ap-south-1` by default,
embeds no credentials, requires no `--capabilities` flag (no IAM
resources). Deployed via a previewed, Add-only change set (one resource:
`ObservabilityDashboard`) — confirmed live: stack `CREATE_COMPLETE`,
dashboard retrievable via `cloudwatch get-dashboard`, deployed body
byte-parses to the same 10-widget structure as the template.

## CloudWatch Alerting / SNS Escalation (Step 31)

**Stack**: `sentinelgrid-observability-alerting` — a dedicated stack
containing exactly two resources: one `AWS::SNS::Topic` and one
`AWS::CloudWatch::Alarm`. Implements SRS FR-7.3 ("CloudWatch alarm/SNS
notification when unresolved faults remain"). Creates no new metrics, no
IAM resources, and does not modify any other stack (including the
dashboard — its template hash is guarded by a standing test to catch any
unintended edit).

- **SNS topic**: `SentinelGrid-UnresolvedFaults`
  (`arn:aws:sns:ap-south-1:401429382000:SentinelGrid-UnresolvedFaults`).
- **CloudWatch alarm**: `SentinelGrid-UnresolvedFaults-Alarm`.
- **Metric watched**: `SentinelGrid` / `UnresolvedFaultCount`, `Sum` over
  a 300-second period (parameterized as `AlarmPeriodSeconds`, default
  300), `EvaluationPeriods=1`, `Threshold=1` (parameterized as
  `AlarmThreshold`), `ComparisonOperator=GreaterThanOrEqualToThreshold`,
  `TreatMissingData=notBreaching`.
- **Action**: `AlarmActions` → the SNS topic only. No `OKActions`, no
  `InsufficientDataActions` — escalating on `ALARM` is the only required
  behavior for this step.

### Why one alarm bound to Subsystem=power/FaultType=NODE_OVERLOAD, not an unbounded aggregate

`UnresolvedFaultCount` always carries both `Subsystem` and `FaultType`
dimensions — a classic single-metric `AWS::CloudWatch::Alarm` requires
an *exact*, fixed dimension set; there is no "any dimension combination"
option on that resource shape. The only way to alarm across an unbounded
set of dimension combinations is a Metric Math alarm using a `SEARCH`
expression wrapped in an aggregating function (e.g. `SUM(SEARCH(...))`)
— this was inspected and considered, but rejected here in favor of the
plain single-metric alarm: Step 31 explicitly asks for a "simple and
deterministic" alarm that avoids metric math "unless necessary," and a
plain alarm is far easier to explain and reason about for a class
project than a search-expression alarm. The alarm is bound to
`Subsystem=power`/`FaultType=NODE_OVERLOAD` specifically because that is
the **only** dimension combination this project has ever produced live
data for (proven in Steps 23/27/29) — not a guess at a future value.
This is a real, acknowledged limitation, documented rather than hidden:
an unresolved fault for `water`/`traffic`, or for any fault type other
than `NODE_OVERLOAD`, would **not** trigger this alarm today. Widening
coverage (either more bounded alarms, or a Metric-Math aggregate alarm)
is left to a later step, once there is real evidence of which
combinations actually need it.

### Event/count semantics — what the alarm does and does NOT mean

**`UnresolvedFaultCount` is an event/count metric. The alarm indicates
that an unresolved-fault observation occurred during the evaluation
period; it does not by itself represent persistent active-fault state.**
A `Sum >= 1` data point means "at least one unresolved-fault observation
was emitted in this 5-minute window" — not "a fault is still
unresolved right now." Correspondingly, the alarm returning to `OK`
means no *new* unresolved-fault observation occurred in the most recent
period — it does not mean any previously-observed fault was resolved
(this system currently has no lifecycle point that marks a fault
resolved; see Step 28's own `UnresolvedFaultCount` limitation note).
`TreatMissingData=notBreaching` reflects this: the total absence of
unresolved-fault data is the normal, healthy state, not something to
alarm on.

### SNS subscription decision

**Step 31 does not create a notification subscription. A verified
endpoint must be subscribed separately.** No email, SMS, or HTTP
endpoint is created, invented, or hardcoded — doing so would require
either fabricating a destination or asking for a personal address to be
committed to source, both of which this step deliberately avoids. The
topic exists with zero subscriptions (confirmed live). To receive real
notifications, subscribe a verified endpoint manually, e.g.:

```bash
aws sns subscribe --topic-arn <TopicArn> --protocol email \
  --notification-endpoint <your-verified-email> \
  --profile sentinelgrid --region ap-south-1
```

...then confirm the subscription from the email AWS sends. This
`deploy-observability-alerting.sh` script prints this exact example as
guidance; it never runs it.

### IAM / no application-code dependency

CloudWatch Alarm → SNS is same-account, service-to-service behavior —
no SNS topic policy resource was created (AWS's own default,
account-scoped topic policy already permits this), and no Lambda
execution role was touched. This stack has zero dependency on, and zero
effect on, any Lambda, IoT rule, SQS queue, or the Step Functions state
machine.

### Deploying

```bash
./infrastructure/deploy-observability-alerting.sh
```

Uses `AWS_PROFILE=sentinelgrid` and `AWS_REGION=ap-south-1` by default,
embeds no credentials, requires no `--capabilities` flag (no IAM
resources). Deployed via a previewed, Add-only change set (two
resources: `UnresolvedFaultsTopic`, `UnresolvedFaultsAlarm`) — confirmed
live: stack `CREATE_COMPLETE`, alarm configuration matches the template
exactly, initial `StateValue=INSUFFICIENT_DATA` (never forced into
`ALARM`), topic has zero subscriptions.

## Persistent Recovery History (Step 33)

**Stack**: `sentinelgrid-recovery-events-storage` — a dedicated stack
containing exactly one resource, `AWS::DynamoDB::Table`
(`SentinelGridRecoveryEvents`). Addresses the SRS Knowledge Base
requirement for persistent fault/recovery history, alongside the
existing `SentinelGridTelemetry` (raw readings) and
`SentinelGridAnomalyEvents` (detected anomalies) tables — this table
adds the third leg: what the remediation workflow actually *did* about
each anomaly.

**This is recovery-EVENT HISTORY, not a current-node-state model.**
Each item is one immutable, historical record of a single
`(node_id, timestamp)` detection's recovery/escalation outcome — the
table is never updated in place, and nothing in this project treats it
as "node X's current status." Step 33 also does not implement a query
API/interface over this table (a later step) and does not implement
FR-6.1 current-node-state (a separate concern).

### Schema

Partition key `node_id` (String), sort key `timestamp` (String) — the
**detection** timestamp (matching `SentinelGridAnomalyEvents`' own sort
key), not the wall-clock moment the recovery Lambda ran. Attributes:

| Field | Meaning |
|---|---|
| `subsystem`, `fault_type`, `severity` | From the originating `DetectionResult`/`RemediationPlan` |
| `action` | The `ActionType` value chosen (e.g. `REDUCE_LOAD`) |
| `automatic` | Whether the plan was eligible for automatic remediation |
| `status` | `SUCCESS` \| `FAILURE` \| `ESCALATION_REQUIRED` (see below) |
| `success` | Boolean outcome — `true` only for `SUCCESS` |
| `reason` | Free-text explanation (from the plan/result's own `reason`/`message`) |
| `recovery_started_at` | Equal to the detection timestamp — the only "recovery start" reference available without an ASL change |
| `recorded_at` | Wall-clock ISO-8601 time this Lambda actually wrote the item |

**Status semantics**: `SUCCESS` — automatic remediation executed and
succeeded (`success=true`). `FAILURE` — an automatic-remediation attempt
that could not execute (a mechanical failure, e.g. an unsupported
action — currently only reachable defensively, not via the real
deployed policy). `ESCALATION_REQUIRED` — the fault needs manual
intervention; `automatic=false`, `success=false`; never claimed as a
successful recovery.

### Who writes what

`lambda_functions/remediation/recovery_event_store.py` (the one
AWS-facing module in that package — mirrors
`lambda_functions/anomaly_detector/event_store.py`'s role exactly).
Ownership is mutually exclusive per event, exactly like Step 28's
`UnresolvedFaultCount` metric split: **Planner** persists an
`ESCALATION_REQUIRED` event when it decides `escalation_required=True`
(the same branch `CheckAutomaticRemediation` routes straight to
`Escalate` from, without ever invoking Executor). **Executor** persists
`SUCCESS`/`FAILURE`/`ESCALATION_REQUIRED` for every plan it actually
receives (always `automatic=true`, per that same ASL routing), except
`SKIPPED` (nothing required, nothing recorded). **Verifier does not
persist** — it has no execution role permission for this table at all.

### Idempotency

The natural key (`node_id`, detection `timestamp`) makes a retried
Lambda invocation produce an identical key. A conditional `PutItem`
(`attribute_not_exists`) makes a true retry an idempotent no-op. Beyond
that, this store reads the existing item back on a
`ConditionalCheckFailedException` and compares the fields that actually
describe the outcome (`status`, `success`, `automatic`, `action`,
`fault_type`, `severity`): identical → idempotent success, logged, no
duplicate; different → `RecoveryEventConflictError` is raised and the
existing record is never overwritten — historical evidence is never
destroyed.

### Failure semantics

Persistence failures are never silently swallowed — they propagate as
`RecoveryEventPersistenceError`, matching `event_store.py`'s own
fail-fast convention. **Deliberate, documented consequence for Planner
specifically**: the existing (unmodified) ASL's `PlanRemediation` Task
has no `Catch` block, so a persistence failure there fails that Step
Functions execution outright (`FAILED`) rather than gracefully
escalating — a considered trade-off favoring "never silently lose
historical evidence" over "always complete gracefully," consistent with
this project's dominant fail-fast persistence pattern. Executor's
persistence failures, by contrast, are caught by the ASL's own existing
`ExecuteRemediation` → `Escalate` `Catch` block — no new resilience
mechanism was needed there.

### Step Functions ASL — unchanged

`step_functions/remediation_workflow.json` was not modified by Step 33
(SHA-256 unchanged, guarded by a standing regression test). Both writers
build their recovery event entirely from information already in scope —
`execution_name`/`execution_arn` are consequently NOT persisted (only
available via the Step Functions Context Object, which would require an
ASL change) — a documented limitation, not an oversight.

### IAM

Planner's and Executor's execution roles each gained one new inline
policy: `dynamodb:PutItem` + `dynamodb:GetItem` (the latter needed
specifically for the idempotency conflict check above), scoped to the
recovery-events table's exact ARN (imported via `!ImportValue`, never
hardcoded). No `Query`/`Scan`/`UpdateItem`/`DeleteItem`, no wildcard
resource. Verifier's role is untouched.

### Deploying

```bash
./infrastructure/deploy-recovery-events-storage.sh
```

Then redeploy `infrastructure/deploy-remediation-lambdas.sh` (packaging
now includes `recovery_event_store.py` in all three zips — required
transitively via `lambda_functions/remediation/__init__.py`'s eager
imports, even though `verifier.py` itself never calls it — the same
ripple-effect this project hit once before with `event_store.py`).
Deployed via previewed, Modify-only change sets on the existing
`sentinelgrid-recovery-events-storage` (Outputs-only, zero resource
changes) and `sentinelgrid-remediation-lambdas` (5 resources: Planner/
Executor roles + functions, Verifier function only) stacks — confirmed
live: table `ACTIVE`/`PAY_PER_REQUEST`/encrypted, both live-validated
controlled executions each produced exactly one correctly-shaped
recovery-event record.

## Persistent Current Node State (Step 34)

**Stack**: `sentinelgrid-node-state-storage` — a dedicated stack
containing exactly one resource, `AWS::DynamoDB::Table`
(`SentinelGridNodeState`). Addresses SRS FR-6.1 ("Maintain current node
state in DynamoDB").

**`SentinelGridNodeState` represents the LATEST KNOWN STATE — exactly
one item per node — NOT an event history table.** Do not confuse it
with `SentinelGridRecoveryEvents` (Step 33), which records historical
recovery/escalation EVENTS (append-only, `(node_id, timestamp)` keyed).
There are now four distinct concepts in this project, never mixed:

| Table | Concept | Key |
|---|---|---|
| `SentinelGridTelemetry` | Raw telemetry history | `(node_id, timestamp)` |
| `SentinelGridAnomalyEvents` | Detected anomaly history | `(node_id, timestamp)` |
| `SentinelGridRecoveryEvents` | Recovery/escalation outcome history | `(node_id, timestamp)` |
| `SentinelGridNodeState` | **Latest known state, one row per node** | `node_id` only |

### Schema and state vocabulary

Partition key `node_id` only — no sort key. Fields: `subsystem`,
`status` (`ONLINE`/`OFFLINE`, the telemetry payload's own field, passed
through unchanged), `state`, `fault_type`/`fault_severity` (absent when
healthy/recovered — REMOVEd, never written as `null`), `reason`,
`last_telemetry_timestamp` (the ordering key — see below),
`last_state_update` (wall-clock write time).

**State vocabulary** (`shared/node_state_store.py`'s `NodeState` enum):
`HEALTHY`, `FAULT_DETECTED`, `RECOVERING`, `RECOVERED`,
`ESCALATION_REQUIRED`, `UNKNOWN` (reserved, not currently written by
any owner).

### Telemetry ordering / stale-write protection

Every write — from the Analyzer OR any remediation Lambda — is an
atomic, conditional `UpdateItem`:
`attribute_not_exists(node_id) OR :event_timestamp >= last_telemetry_timestamp`.
One condition serves two purposes: (1) a late-arriving OLDER telemetry
message can never regress a newer one; (2) a remediation Lambda's
`RECOVERING`/`RECOVERED`/`ESCALATION_REQUIRED` write — keyed by the
ORIGINATING detection's timestamp, unchanged through the workflow — is
correctly rejected as stale if a genuinely newer telemetry event has
already moved the node's state forward past that point. `>=` (not
strict `>`) makes a same-timestamp retry an idempotent, harmless
overwrite rather than a rejection. Live-verified (Step 34 Live Test E):
publishing a newer HEALTHY telemetry event, then an older
NODE_OVERLOAD-triggering one, left the node's `last_state_update`
completely unchanged — the older write was silently, correctly rejected
— while the older event's own anomaly detection/persistence still
proceeded independently and correctly (staleness protection is scoped
to the node-state table only, never to detection or anomaly history).

### State-transition ownership

| Writer | Trigger | State written |
|---|---|---|
| Analyzer (`handler.py`) | `detected=False`, usable identity | `HEALTHY` |
| Analyzer (`handler.py`) | `detected=True`, usable identity | `FAULT_DETECTED` |
| Planner (`planner.py`) | `automatic=True` | `RECOVERING` |
| Planner (`planner.py`) | `escalation_required=True` | `ESCALATION_REQUIRED` |
| Executor (`executor.py`) | `ExecutionStatus.SUCCESS` | `RECOVERED` |
| Executor (`executor.py`) | `ExecutionStatus.REJECTED` | `FAULT_DETECTED` |
| Executor (`executor.py`) | `ExecutionStatus.ESCALATED` (defensive) | `ESCALATION_REQUIRED` |
| Verifier (`verifier.py`) | `verified=False` (defensive backstop) | `ESCALATION_REQUIRED` |

Executor writes `RECOVERED` immediately on `SUCCESS` (not deferred to
Verifier) because the current, unmodified `RemediationEngine` guarantees
a `SUCCESS` result's identity fields always match the plan that produced
it — `verified=False` cannot currently follow a genuine `SUCCESS`.
Verifier's own `ESCALATION_REQUIRED` write exists as a defensive
backstop against that ever becoming untrue, so "never falsely mark an
escalated fault as RECOVERED" holds unconditionally, not just today.

### Idempotency

The table itself enforces one item per node (partition-key-only design)
— there is no possibility of a duplicate row. A repeated write for the
identical `(node_id, event_timestamp)` pair is accepted by the `>=`
condition and simply re-applies the same (deterministically identical)
values — never an error, never a duplicate.

### Failure semantics — best-effort, not fail-fast

Unlike `event_store.py`/`recovery_event_store.py` (fail-fast), node-state
persistence is deliberately best-effort: a missed update is a
correctable snapshot gap (superseded by the next telemetry event or
workflow step), not irreplaceable historical evidence. This also
eliminates by construction the risk Step 34 specifically warns about — a
state-persistence failure causing an Analyzer/remediation Lambda retry
that creates a DUPLICATE anomaly record or remediation execution —
since this module never raises, it can never trigger that retry. Every
failure is still logged at ERROR (not silently swallowed).

### IAM

Analyzer, Planner, Executor, and Verifier each hold exactly one
additional `dynamodb:UpdateItem` grant, scoped to the node-state table's
exact ARN (via `!ImportValue`) — no `PutItem`/`GetItem`/`Query`/`Scan`/
`DeleteItem`, no wildcard resource. `NODE_STATE_TABLE_NAME` is populated
via `!ImportValue` in every function's environment, never hardcoded a
second time in source.

### Deploying

```bash
./infrastructure/deploy-node-state-storage.sh
```

Then redeploy `deploy-anomaly-detector.sh` and
`deploy-remediation-lambdas.sh` (packaging now includes
`shared/node_state_store.py` in all four Lambda zips). Deployed via
previewed, Modify-only change sets — confirmed live: table `ACTIVE`/
`PAY_PER_REQUEST`/encrypted/`node_id`-only key; all four Lambdas'
environment variables and IAM policies verified; all four deployed
packages confirmed to contain `shared/node_state_store.py`.

## Knowledge Base Query Interface (Step 35)

Implements SRS FR-6.3: "Provide a query interface for retrieving current
node state and fault/recovery history." Steps 33/34 built the persistent
Knowledge Base itself (`SentinelGridNodeState`, `SentinelGridAnomalyEvents`,
`SentinelGridRecoveryEvents`); Step 35 adds the first way to *read* it
without going through the AWS CLI/console directly:

```
User / operator
    -> API Gateway HTTP API (SentinelGridKnowledgeQueryApi, $default stage)
    -> SentinelGridKnowledgeQuery Lambda (lambda_functions/knowledge_query/)
    -> DynamoDB: GetItem(NodeState) / Query(AnomalyEvents) / Query(RecoveryEvents)
```

This is a **new, independent read path** — it is not wired into the
telemetry or remediation data path in any way. Nothing in the existing
IoT -> SQS -> Analyzer -> Planner/Executor/Verifier -> Knowledge Base
write pipeline changed to build this; this API is purely an additional
consumer of the same three tables those Lambdas already write to.

### Resources created (`infrastructure/knowledge-query.yaml`)

| Resource | Type | Purpose |
|---|---|---|
| `KnowledgeQueryExecutionRole` | `AWS::IAM::Role` | Logging + read-only DynamoDB access (see IAM below). |
| `KnowledgeQueryFunction` | `AWS::Lambda::Function` (`SentinelGridKnowledgeQuery`) | The one Lambda serving all four routes. |
| `KnowledgeQueryApi` | `AWS::ApiGatewayV2::Api` | HTTP API (not REST API — see "Why HTTP API" below), no CORS configuration. |
| `KnowledgeQueryIntegration` | `AWS::ApiGatewayV2::Integration` | `AWS_PROXY` integration, payload format 2.0. |
| `KnowledgeQueryRoute{CurrentState,Anomalies,Recoveries,Summary}` | `AWS::ApiGatewayV2::Route` (x4) | The four GET routes — see below. No POST/PUT/PATCH/DELETE route exists anywhere in this API. |
| `KnowledgeQueryStage` | `AWS::ApiGatewayV2::Stage` | `$default` stage, auto-deployed. |
| `KnowledgeQueryInvokePermission` | `AWS::Lambda::Permission` | Grants API Gateway invoke rights, scoped to this API's own `execute-api` ARN + the `nodes*` resource-path prefix — not a public/wildcard grant. |

No DynamoDB table is created here — this stack only *reads* the three
tables `node-state-storage.yaml`/`anomaly-events-storage.yaml`/
`recovery-events-storage.yaml` already created.

### Why API Gateway HTTP API, not REST API

HTTP API is the lower-cost, simpler-to-configure API Gateway product for
a plain Lambda-proxy read API with no need for REST API-only features
(request validators, usage plans, WAF association, custom authorizers
beyond what HTTP API already supports) — matching this project's
low-cost, serverless-only design constraint. No EC2/ECS/EKS/RDS/always-on
server, no GraphQL/AppSync, no additional Lambda-per-query, no frontend,
no custom domain, no Route 53, no WAF.

### Endpoints

| Method | Path | Returns |
|---|---|---|
| `GET` | `/nodes/{node_id}` | Current state (`SentinelGridNodeState`, `GetItem`). |
| `GET` | `/nodes/{node_id}/anomalies` | Anomaly history (`SentinelGridAnomalyEvents`, `Query`). |
| `GET` | `/nodes/{node_id}/recoveries` | Recovery history (`SentinelGridRecoveryEvents`, `Query`). |
| `GET` | `/nodes/{node_id}/summary` | Combined view: current state + both histories. |

No other endpoint exists. There is no POST/PUT/PATCH/DELETE route in the
HTTP API at all — an unsupported method for `/nodes/...` never reaches
the Lambda; API Gateway itself returns its own 404 (no matching route,
and no catch-all `$default` route is defined), so the Lambda's read-only
guarantee is enforced at the routing layer, not only in application code.

### Request parameters

- `node_id` (path, required on every route): trimmed of surrounding
  whitespace, must be non-empty, max 128 characters. Never used to build
  a DynamoDB expression string — it only ever reaches DynamoDB as an
  `ExpressionAttributeValues` placeholder, so it cannot inject arbitrary
  DynamoDB syntax regardless of content.
- `limit` (query, optional; `/anomalies`, `/recoveries`, `/summary` only):
  integer, default `20`, minimum `1`, maximum `100`. Out-of-range or
  non-integer values are rejected with `400 INVALID_LIMIT` rather than
  silently clamped.
- `order` (query, optional; same three routes): `asc` or `desc`, default
  `desc` (newest first). Any other value is rejected with
  `400 INVALID_ORDER`.

No table name, DynamoDB expression, projection, or filter is ever
accepted from a request — the query surface is fixed by this Lambda's
own code, not caller-controlled.

### Response shapes

Current state (`200`):
```json
{"node": {"node_id": "...", "subsystem": "power", "status": "ONLINE", "state": "HEALTHY", "last_telemetry_timestamp": "...", "last_state_update": "...", "reason": "..."}}
```

Missing node (`404`):
```json
{"error": "NODE_NOT_FOUND", "message": "No current state exists for the requested node."}
```

Anomaly/recovery history (`200`):
```json
{"node_id": "power-001", "count": 2, "items": [{"...": "..."}]}
```

Summary (`200`):
```json
{"node_id": "power-001", "current_state": {"...": "..."}, "anomalies": {"count": 2, "items": [...]}, "recoveries": {"count": 1, "items": [...]}}
```

Every `Decimal` value DynamoDB returns is converted to a plain `int`/
`float` before serialization — no DynamoDB-internal type wrapper is ever
present in a response body (see `query_service._to_native_json`).

### Current-state vs. history — what this table distinction means for queries

- **`SentinelGridNodeState`** — the *latest known* state, one item per
  node, `GetItem` only. `GET /nodes/{node_id}` and the `current_state`
  half of `/summary` read only this.
- **`SentinelGridAnomalyEvents`** / **`SentinelGridRecoveryEvents`** —
  append-only historical records, `(node_id, timestamp)` keyed, `Query`
  only, ordered/limited by DynamoDB itself (`ScanIndexForward`/`Limit`) —
  never fetched in full and re-sorted in Python. This interface never
  treats `SentinelGridNodeState` as history, and never treats the two
  event tables as a current-state model — see Step 33/34's own README
  sections above for why they are deliberately separate tables.

### Ordering and limiting

`ScanIndexForward=False` (default, `order=desc`) returns newest-first;
`ScanIndexForward=True` (`order=asc`) returns oldest-first. `Limit` is
passed straight to `Query`, so at most `limit` items are ever read off a
table for one request — this service never Scans, and never fetches more
than requested to sort/truncate client-side.

### Read-only guarantee

`lambda_functions/knowledge_query/query_service.py` contains **no** call
to `put_item`/`update_item`/`delete_item`/`batch_write_item` anywhere —
enforced by a dedicated static source-scan test
(`tests/test_knowledge_query_service.py::test_query_service_module_never_calls_any_write_operation`)
that fails the build if one is ever added, on top of IAM only granting
`GetItem`/`Query`. Two independent layers (application code + IAM) both
guarantee this Lambda can never modify the Knowledge Base:

**This interface is read-only. It does not modify the Knowledge Base.**

### IAM

`KnowledgeQueryExecutionRole` grants exactly:
- `dynamodb:GetItem` on `SentinelGridNodeState`'s exact ARN.
- `dynamodb:Query` on `SentinelGridAnomalyEvents`'s exact ARN.
- `dynamodb:Query` on `SentinelGridRecoveryEvents`'s exact ARN.
- CloudWatch Logs (`CreateLogGroup`/`CreateLogStream`/`PutLogEvents`),
  scoped to this Lambda's own log group.

No `PutItem`/`UpdateItem`/`DeleteItem`/`Scan`/`dynamodb:*`, no wildcard
`Resource`. All three table ARNs come from `!ImportValue`, never a
hardcoded ARN. No GSI/LSI exists on any of the three tables, so no
separate index ARN is needed for `Query`.

### Environment variables

`NODE_STATE_TABLE_NAME`, `ANOMALY_EVENTS_TABLE_NAME`,
`RECOVERY_EVENTS_TABLE_NAME` — all populated via `!ImportValue`, never
hardcoded in source beyond `query_service.py`'s own documented fallback
defaults (used only if an environment variable is genuinely absent, e.g.
local testing).

### Security / authentication limitation

**No authentication or authorization is implemented on this API.** Adding
Cognito (not already present anywhere in this project) or a custom
authorizer would be a substantial new subsystem, explicitly out of scope
per Step 35's own instructions unless a genuine architectural blocker
required it — none did. The interface is READ-ONLY (no way to modify the
Knowledge Base even if called by an unintended caller) and exposes no
credentials, secrets, or write capability, which bounds the impact of
this gap, but the HTTP API endpoint itself is public/unauthenticated once
deployed. This is a known, explicitly documented limitation, not an
oversight — **do not treat this as a production-ready public API.** A
future step could add an API Gateway authorizer (IAM, Lambda, or JWT) if
real access control becomes a requirement; none is implemented today.

### CORS

Deliberately **not enabled**. No browser frontend exists anywhere in this
project (see the rest of this README) — this API is intended for direct,
non-browser callers (`curl`, `aws lambda invoke`, a script). Enabling
wildcard CORS would be unused surface area with no current requirement.

### Deploying

Requires `infrastructure/anomaly-events-storage.yaml`'s
`SentinelGridAnomalyEventsTableName` export (added in this same step — a
purely additive change to that existing stack's `Outputs`, not its
`AnomalyEventsTable` resource) to already be deployed, in addition to the
pre-existing `SentinelGridNodeState*`/`SentinelGridRecoveryEvents*`
exports from Steps 33/34.

```bash
DEPLOYMENT_BUCKET=<existing-versioned-bucket> ./infrastructure/deploy-knowledge-query.sh
```

Packaging (`infrastructure/package-knowledge-query.sh`) bundles only
`lambda_functions/knowledge_query/` — confirmed by inspection to need
nothing from `shared/`, `remediation/`, `lambda_functions/anomaly_detector/`,
or `lambda_functions/remediation/`; this is a fully self-contained
package, unlike the detector/remediation Lambdas.

### What Step 35 does NOT implement

No authentication/authorization beyond what's described above, no
MTTR/detection-accuracy/false-positive-rate computation, no 500-node
scale validation, no NFR-1/NFR-2 measurement, no dashboard integration —
none of these were touched or claimed. This step implements exactly
FR-6.3: a query interface for current node state and fault/recovery
history.

## Fleet Listing & CORS (Step 40)

Makes the Knowledge Query API (Step 35 above) usable by the project's
frontend (`frontend/`, Step 39): adds a read-only fleet-listing route and
enables CORS. Both changes are to the SAME existing stack
(`sentinelgrid-knowledge-query`) and the SAME existing API — no new API
Gateway, no new Lambda, no new DynamoDB table. `infrastructure/knowledge-query.yaml`
is the only template modified; no other stack was touched.

### `GET /nodes`

Added alongside the four existing routes (unchanged). Bounded, read-only
DynamoDB `Scan` over `SentinelGridNodeState` — see
`lambda_functions/knowledge_query/README.md`'s own `GET /nodes` section
for the full request/response contract, and
`lambda_functions/knowledge_query/query_service.py`'s `list_nodes()`
docstring for the complete reasoning. Summarized here:

- **Why `Scan`, not `Query`**: `SentinelGridNodeState`'s only key is
  `node_id` (see `node-state-storage.yaml`) — there is no secondary index
  a fleet-wide read could `Query` against. Creating a GSI was considered
  and deliberately rejected for this change: real, standing
  infrastructure and cost for a single bounded read pattern, not
  justified at this project's target scale (SRS NFR-3, ~500 nodes).
- **Bounded, not unbounded**: `Limit` is always passed to `Scan`, capped
  at `MAX_LIST_LIMIT` = 500 — never an arbitrary caller-supplied value,
  never a full unbounded table scan.
- **`order` means `node_id` sort, not recency** — `Scan` has no native
  ordering; the Lambda sorts the already-bounded page in Python. Do not
  read `order=desc` here as "newest telemetry first."
- **Filtering caveat, live-verified**: `subsystem`/`state` filters use a
  `FilterExpression` applied AFTER the bounded `Scan`, so a filtered
  response can under-count true matches beyond what one `Scan` examined.
  Live-verified directly against the deployed table (637 items at the
  time): `GET /nodes?subsystem=traffic&limit=3` returned `count: 0`
  (the first 3 items scanned happened to be non-traffic), while
  `GET /nodes?subsystem=traffic&limit=500` returned `count: 136` against
  a true DynamoDB-side total of 166 matching items (`aws dynamodb scan
  --filter-expression "subsystem = :s" --select COUNT`) — both entirely
  consistent with the documented behavior, not a bug.

### CORS

`KnowledgeQueryApi.CorsConfiguration` (an `AWS::ApiGatewayV2::Api`
property — HTTP APIs handle CORS, including the preflight `OPTIONS`
request, entirely inside API Gateway; no extra route, no extra Lambda
invocation, no application code involved):

```yaml
CorsConfiguration:
  AllowOrigins:
    - !Ref FrontendOrigin
  AllowMethods:
    - GET
  AllowHeaders:
    - Content-Type
  MaxAge: 300
```

- **`FrontendOrigin`** — a new template `Parameter`, default
  `http://localhost:5173` (the frontend's local Vite dev server origin).
  Deliberately one explicit origin, never `"*"` — override it at deploy
  time once the frontend is ever actually hosted somewhere (not done by
  this template, which does not deploy or host the frontend).
- **`AllowMethods: [GET]` only** — there is no other method on this API
  to allow.
- **No `AllowCredentials`** — this API uses no cookies/credentials
  (unauthenticated by design, unchanged — see below), so it is correctly
  omitted (defaults to `false`).
- **Live-verified** (both checked directly against the deployed API,
  with `Origin: http://localhost:5173`):
  - Preflight `OPTIONS /nodes` → `204`, with
    `access-control-allow-origin: http://localhost:5173`,
    `access-control-allow-methods: GET`,
    `access-control-allow-headers: content-type`,
    `access-control-max-age: 300`.
  - The actual `GET /nodes?limit=1` response itself also carries
    `access-control-allow-origin: http://localhost:5173`, which is what a
    browser's `fetch()` actually checks before exposing the response body
    to JavaScript.

**Still unauthenticated, by design** — enabling CORS makes this API
reachable from a browser tab; it adds no authentication or authorization.
This is the same explicitly-documented limitation as Step 35, carried
forward unchanged — see "Knowledge Base Query Interface (Step 35)"
above's own "Security / authentication limitation" note. The interface
remains strictly read-only (unchanged guarantee — see that section too),
which bounds the impact.

### IAM (Step 40 change)

`KnowledgeQueryExecutionRole`'s `SentinelGridKnowledgeQueryNodeStateRead`
policy now grants `dynamodb:GetItem` **and** `dynamodb:Scan` (previously
`GetItem` only), still scoped to exactly `SentinelGridNodeState`'s table
ARN via `!ImportValue` — no other table gained a new permission, no
wildcard `Resource`, no write action anywhere. Live-verified after
deployment:

```json
{"Action": ["dynamodb:GetItem", "dynamodb:Scan"], "Resource": ["arn:aws:dynamodb:ap-south-1:401429382000:table/SentinelGridNodeState"], "Effect": "Allow"}
```

The `SentinelGridKnowledgeQueryAnomalyEventsRead`/
`SentinelGridKnowledgeQueryRecoveryEventsRead` policies (Query-only, on
their own respective tables) and the logging policy are unchanged —
verified live, byte-identical to before this change.

### Deployment record

Deployed via a previewed `UPDATE` change set to the existing
`sentinelgrid-knowledge-query` stack (not a new stack): 4 `Modify`
(no `Replacement`) + 1 `Add` (`KnowledgeQueryRouteListNodes`) — no
resource removed, no resource replaced. The API ID
(`m1uqr1hsq3`) and endpoint
(`https://m1uqr1hsq3.execute-api.ap-south-1.amazonaws.com`) are
unchanged. The deployed Lambda package was downloaded and confirmed
byte-size-identical to the local build, and imports cleanly in isolation
with the new `list_nodes()`/`GET /nodes` code present.

### What Step 40 does NOT implement

No authentication/authorization (see above), no pagination beyond one
bounded `Scan` (no `LastEvaluatedKey` follow-up), no GSI, no Dashboard/
Events/Analytics live-data wiring (only the Nodes page — see
`frontend/README.md`), no Lambda concurrency work, no 500-node
scalability test, no frontend deployment.

## Fleet-Wide Event Feed (Step 42)

Adds a bounded, read-only, fleet-wide event feed merging
`SentinelGridAnomalyEvents` and `SentinelGridRecoveryEvents` into one
normalized `GET /events` route, and connects the frontend's Events page
to it. Same existing stack (`sentinelgrid-knowledge-query`) and the same
existing API as Steps 35/40 — no new API Gateway, no new Lambda, no new
DynamoDB table, no GSI. `infrastructure/knowledge-query.yaml` is the
only template modified; no other stack was touched.

### `GET /events`

Added alongside the five existing routes (all unchanged). See
`lambda_functions/knowledge_query/README.md`'s own `GET /events` section
for the full request/response contract, and
`lambda_functions/knowledge_query/query_service.py`'s `list_events()`
docstring for the complete reasoning. Summarized here:

- **Why `Scan` (twice), not `Query`**: both event tables are keyed by
  `(node_id, timestamp)` only (see `anomaly-events-storage.yaml` /
  `recovery-events-storage.yaml`) — there is no fleet-wide secondary
  index either could `Query` against for "the N most recent events across
  the whole fleet." A GSI was considered and deliberately rejected for
  the same reason as Step 40's `GET /nodes`: real, standing
  infrastructure and cost, not justified at this project's current scale
  (tens of items per table — see live counts below).
- **Bounded, not unbounded**: each table's `Scan` always passes an
  internal `Limit` of `_EVENT_TABLE_SCAN_LIMIT` = 500 — a fixed constant,
  never client-controlled, reusing the same SRS NFR-3 justification as
  `MAX_LIST_LIMIT`. The client-facing `limit` query parameter (default
  50, max 100) is a separate, smaller, independently-validated bound
  applied only after both tables' results are merged and sorted.
- **Merge-then-sort-in-Python, and an explicit honesty limitation**: each
  table is `Scan`ned independently (skipping a table entirely when
  `event_type` narrows to the other kind), each item normalized onto one
  flat schema, the two result sets concatenated, then sorted by each
  event's own `timestamp` in Python. This is **not** a claim that `GET
  /events` returns a globally-complete or provably-correct
  chronological history — once a table exceeds the internal 500-item
  Scan bound, a single `Scan` call has no guarantee of examining that
  table's true most-recent items first. At this project's current live
  table sizes this has no practical effect (see counts below), but
  neither the code nor this document relies on table size staying small
  to remain honest about it.
- **`status` is recovery-only**: no `status` attribute exists on
  `SentinelGridAnomalyEvents` items, so it is never passed to that
  table's `Scan`. `event_type=anomaly` combined with any `status` value
  is rejected with `400 INVALID_STATUS` before the service layer runs.

### CORS

**No changes.** `GET /events` is covered automatically by the same
API-level `CorsConfiguration` Step 40 added (CORS in an HTTP API is
per-API, not per-route) — re-verified live specifically for `/events`
below, not merely assumed.

### IAM (Step 42 change)

`KnowledgeQueryExecutionRole`'s
`SentinelGridKnowledgeQueryAnomalyEventsRead` and
`SentinelGridKnowledgeQueryRecoveryEventsRead` policies now each grant
`dynamodb:Query` **and** `dynamodb:Scan` (previously `Query` only), each
still scoped to exactly its own table's ARN via `!ImportValue` — no
wildcard `Resource`, no write action anywhere, no other table's
permission touched. Live-verified after deployment:

```json
{"Action": ["dynamodb:Query", "dynamodb:Scan"], "Resource": ["arn:aws:dynamodb:ap-south-1:401429382000:table/SentinelGridAnomalyEvents"], "Effect": "Allow"}
{"Action": ["dynamodb:Query", "dynamodb:Scan"], "Resource": ["arn:aws:dynamodb:ap-south-1:401429382000:table/SentinelGridRecoveryEvents"], "Effect": "Allow"}
```

`SentinelGridKnowledgeQueryNodeStateRead` (`GetItem`+`Scan`, Step 40) and
the logging policy are unchanged — verified live, byte-identical to
before this change.

A second, separately-scoped `AWS::Lambda::Permission`
(`KnowledgeQueryInvokePermissionEvents`, `SourceArn` ending `/events*`)
was added rather than broadening the existing Step 35/40 permission
(`SourceArn` ending `/nodes*`) — one permission per top-level resource
path, mirroring the existing least-privilege reasoning exactly rather
than widening its scope. Live-verified: exactly two Lambda resource-policy
statements exist, each scoped to its own path prefix, neither public.

### Deployment record

Deployed via a previewed `UPDATE` change set to the existing
`sentinelgrid-knowledge-query` stack (not a new stack): 3 `Modify`
(`KnowledgeQueryExecutionRole`, `KnowledgeQueryFunction`,
`KnowledgeQueryIntegration`, no `Replacement`) + 2 `Add`
(`KnowledgeQueryRouteListEvents`, `KnowledgeQueryInvokePermissionEvents`)
— no resource removed, no resource replaced. The API ID (`m1uqr1hsq3`)
and endpoint (`https://m1uqr1hsq3.execute-api.ap-south-1.amazonaws.com`)
are unchanged. The deployed Lambda package was downloaded, confirmed
byte-identical (SHA-256) to the local build, and imported cleanly in
isolation with the new `list_events()`/`GET /events` code present; the
downloaded copy was deleted immediately after verification.

**Live API validation** (all against the deployed endpoint):

- `GET /events` → `200`, `count: 33`, real anomaly/recovery events from
  earlier steps' fault-injection/recovery activity (no fabricated IDs or
  timestamps).
- `GET /events?limit=1` → `200`, exactly 1 event.
- `GET /events?event_type=anomaly` / `event_type=recovery` → `200`,
  every returned event's own `event_type` matches the filter.
- `GET /events?subsystem=power` → `200`, every returned event's
  `subsystem` matches the filter.
- `GET /events?limit=999` (invalid — exceeds `MAX_EVENT_LIMIT`) → `400
  INVALID_LIMIT`.
- Regression: `GET /nodes/{known-node-id}/anomalies`,
  `GET /nodes/{known-node-id}/recoveries`, and `GET /nodes?limit=1` all
  still return `200` with unchanged response shapes.
- CORS, live-verified for `/events` specifically: preflight `OPTIONS
  /events` with `Origin: http://localhost:5173` → `204` with
  `access-control-allow-origin: http://localhost:5173`,
  `access-control-allow-methods: GET`,
  `access-control-allow-headers: content-type`,
  `access-control-max-age: 300`; the actual `GET /events` response also
  carries `access-control-allow-origin: http://localhost:5173`; the same
  request with a disallowed `Origin` (`http://evil.example.com`) returns
  no `Access-Control-Allow-Origin` header at all, which is what causes a
  real browser's `fetch()` to block the response from JavaScript.

**Live frontend validation**: the Vite dev server was started locally
(`http://localhost:5173`) and confirmed to serve the `/events` route.
No literal browser/DevTools automation tool is available in this
environment — as disclosed for Steps 40/41 — so verification used
`curl` reproducing the exact request and headers a browser's `fetch()`
would send from that origin (`Origin: http://localhost:5173`, the same
URL and query string `useEvents()`/`getEvents()` construct), confirming
a `200` response carrying the correct CORS header. This is not a
substitute for literal visual browser verification, and is reported
as such rather than claimed as full browser validation.

**Read-only regression check**: exact item counts (via `Scan ... Select
COUNT`, not the periodically-refreshed `describe-table` estimate) for
all three Knowledge Base tables, both SQS queue depths, and the most
recent Step Functions execution were captured before deployment and
re-checked after all live testing above:

| Resource | Before | After |
|---|---|---|
| `SentinelGridNodeState` (item count) | 637 | 637 |
| `SentinelGridAnomalyEvents` (item count) | 17 | 17 |
| `SentinelGridRecoveryEvents` (item count) | 16 | 16 |
| `SentinelGridTelemetryQueue` (approx. messages) | 0 | 0 |
| `SentinelGridTelemetryDLQ` (approx. messages) | 0 | 0 |
| Most recent Step Functions execution | `sentinelgrid-remediation-7fc23e8f75e176e6395e9e7cbaca3f49` (SUCCEEDED) | same execution, unchanged — no new execution |

All 13 other CloudFormation stacks' `LastUpdatedTime` and the
`SentinelGridRemediationWorkflow` state machine's ASL definition
SHA-256 were also confirmed unchanged before/after. No write of any kind
occurred as a result of implementing, deploying, or validating Step 42.

### What Step 42 does NOT implement

No authentication/authorization (see Step 35/40 above), no GSI/fleet-wide
index of any kind, no pagination beyond one bounded internal `Scan` per
table (no `LastEvaluatedKey` follow-up), no guaranteed-complete
historical event query once a table exceeds the internal 500-item Scan
bound (see above), no Dashboard/Analytics live-data wiring (only the
Nodes, Dashboard, and now Events pages — see `frontend/README.md`), no
Lambda concurrency work, no 500-node scalability test, no frontend
deployment, no literal browser/DevTools automation (see "Live frontend
validation" above).

## CloudWatch Metrics Feed (Step 43)

Adds a bounded, read-only feed over the seven approved `SentinelGrid`
CloudWatch metrics (Step 28's own contract — `shared/metrics.py`), and
connects the frontend's Analytics page to it for the first time. Same
existing stack (`sentinelgrid-knowledge-query`) and the same existing API
as Steps 35/40/42 — no new API Gateway, no new Lambda, no new DynamoDB
table, no GSI, and (unlike every prior extension of this stack) no new
DynamoDB permission of any kind — this is the FIRST change to this
stack backed by CloudWatch rather than DynamoDB.
`infrastructure/knowledge-query.yaml` is the only template modified; no
other stack was touched; `shared/metrics.py` (the PUBLISH side) was not
modified at all — metric names, namespace, units, dimensions, and
publishing behavior are all byte-for-byte unchanged.

### `GET /metrics`

Added alongside the six existing routes (all unchanged). See
`lambda_functions/knowledge_query/README.md`'s own `GET /metrics`
section for the full request/response contract, and the new
`lambda_functions/knowledge_query/metrics_service.py`'s own docstring
for the complete reasoning. Summarized here:

- **Approved-metric allowlist, never an arbitrary CloudWatch query** —
  exactly the 7 metrics `shared/metrics.py`'s `MetricName` class already
  defines, exactly the `SentinelGrid` namespace, exactly the
  `Subsystem`/`FaultType` dimensions. No request parameter accepts an
  arbitrary metric name or namespace.
- **No `cloudwatch:ListMetrics` call** — which (Subsystem, FaultType)
  pairs to query is derived from this project's own closed, already-known
  vocabulary (3 subsystems x 5 fault types) rather than discovered, so no
  second CloudWatch read permission was needed.
- **Bounded time window**: default 1 hour, maximum 24 hours, enforced in
  `handler.py` (`400 TIME_RANGE_TOO_LARGE`/`400 INVALID_TIME_RANGE`/`400
  FUTURE_END_TIMESTAMP` as appropriate) — never an arbitrary long
  historical query.
- **CloudWatch period scales with the window (60s / 300s / 900s)** to
  bound how many datapoints one response returns, while never claiming
  sub-minute precision the underlying ~60s-resolution metrics don't
  actually have.
- **Honest empty-series semantics**: a fully-specified (Subsystem AND
  FaultType both given) request returns all 7 metrics even with zero
  datapoints (`datapoints: []`, never a fabricated zero); an
  unfiltered/partially-filtered request only returns series that
  actually have data (see `metrics_service.py`'s own docstring for the
  full "no datapoint is not zero" reasoning Step 43 explicitly required).

### IAM (Step 43 change) — the one genuinely new kind of grant this project has made

`KnowledgeQueryExecutionRole` gained a new inline policy,
`SentinelGridKnowledgeQueryMetricsRead`, granting exactly
`cloudwatch:GetMetricData` (never `PutMetricData`, `PutMetricAlarm`,
`SetAlarmState`, `DeleteAlarms`, or `GetMetricStatistics`) with
`Resource: "*"`.

**Why `Resource: "*"` with no Condition, unlike every DynamoDB grant in
this stack**: `cloudwatch:GetMetricData` has no ARN-level resource
scoping at all — this is a real, documented AWS API limitation, not a
choice. The FIRST deployment of this policy added a `cloudwatch:namespace`
`Condition` (mirroring this project's own Step 28 `PutMetricData`
grants, which use that exact condition key effectively) — **this did
NOT work, discovered via live evidence, not assumed**:

```json
// aws iam simulate-principal-policy, no context supplied (matching a real GetMetricData call):
{"EvalDecision": "implicitDeny", "MatchedStatements": [], "MissingContextValues": ["cloudwatch:namespace"]}
```

and the deployed Lambda's own CloudWatch Logs showed the identical
`AccessDenied` for every real `GET /metrics` request:

```
AccessDenied ... is not authorized to perform: cloudwatch:GetMetricData because no identity-based policy allows the cloudwatch:GetMetricData action
```

**Root cause**: `PutMetricData`/`ListMetrics`/`GetMetricStatistics` each
have a top-level `Namespace` request parameter AWS surfaces as the
`cloudwatch:namespace` IAM condition context key — `GetMetricData`
instead expresses namespace nested inside each
`MetricDataQueries[].MetricStat.Metric.Namespace` entry, which AWS does
NOT surface as that context key. The condition was removed in a second,
immediately-following change set (`Modify`-only, no `Replacement`); after
IAM's normal eventual-consistency propagation delay (observed: on the
order of tens of seconds, confirmed via repeated live `GET /metrics`
polling and re-checked with `aws iam simulate-principal-policy`, which
then returned `"EvalDecision": "allowed"` with no missing context), the
endpoint began working correctly. **Practical consequence, documented
rather than hidden**: IAM cannot narrow this specific grant below
`Resource: "*"` — the application code (`metrics_service.py`'s fixed,
never-caller-controlled `NAMESPACE`/`APPROVED_METRICS` constants) is the
sole enforcement that only the `SentinelGrid` namespace and only the 7
approved metrics are ever queried.

No other IAM statement in this role changed — verified live,
byte-identical to before this change (DynamoDB `GetItem`/`Scan`/`Query`
grants, and the logging policy, all unchanged).

A THIRD, separately-scoped `AWS::Lambda::Permission`
(`KnowledgeQueryInvokePermissionMetrics`, `SourceArn` ending
`/metrics*`) was added, matching the existing "one permission per
top-level resource path" pattern from Steps 40/42 exactly, rather than
folding into either existing permission.

### CORS

**No changes.** `GET /metrics` is covered automatically by the same
API-level `CorsConfiguration` Step 40 added — re-verified live
specifically for `/metrics` below.

### Deployment record

Deployed via two previewed, sequential `UPDATE` change sets to the
existing `sentinelgrid-knowledge-query` stack (not a new stack) — the
second was required only because of the IAM condition-key discovery
above, not a separate infrastructure change:

1. `step43-metrics-feed`: 3 `Modify` (`KnowledgeQueryExecutionRole`,
   `KnowledgeQueryFunction`, `KnowledgeQueryIntegration`, no
   `Replacement`) + 2 `Add` (`KnowledgeQueryRouteMetrics`,
   `KnowledgeQueryInvokePermissionMetrics`) — no resource removed or
   replaced.
2. `step43-metrics-iam-fix`: 3 `Modify` only (same three resources,
   dropping the non-functional `cloudwatch:namespace` Condition from
   `KnowledgeQueryExecutionRole`'s new policy) — no `Add`/`Remove`, no
   `Replacement`.

Both were `UPDATE_COMPLETE`. The API ID (`m1uqr1hsq3`) and endpoint
(`https://m1uqr1hsq3.execute-api.ap-south-1.amazonaws.com`) are
unchanged throughout. The deployed Lambda package (including the new
`metrics_service.py`) was downloaded after the first change set,
confirmed byte-identical (SHA-256) to the local build, and imported
cleanly in isolation with the new `MetricsQueryService`/`GET /metrics`
code present; the downloaded copy was deleted immediately after
verification. `infrastructure/package-knowledge-query.sh` was updated to
bundle `metrics_service.py` alongside the existing three files —
verified present in the built zip.

**Live pre-check** (per Step 43's own explicit instruction, before any
`GET /metrics` testing): `aws cloudwatch list-metrics --namespace
SentinelGrid` confirmed 6 real, already-published metric series exist
(`FaultDetectionCount`, `DetectionLatency`, `RecoveryAttemptCount`,
`RecoveryLatency`, `RecoverySuccessCount`, `UnresolvedFaultCount`, all
dimensioned `Subsystem=power, FaultType=NODE_OVERLOAD` — `RecoveryFailureCount`
has never been published, consistent with this project's escalation
events having gone through `UnresolvedFaultCount` rather than a rejected/
failed execution). No metric was manually published to manufacture test
data — every datapoint used for validation below is real, pre-existing
data from earlier steps' fault-injection/recovery activity (roughly 23
hours old at validation time).

**Live API validation** (all against the deployed endpoint):

- `GET /metrics` (default, last 1 hour) → `200`, `series: []` — an
  HONEST empty result, since the real historical data is ~23 hours old
  and therefore outside the default 1-hour window (not a bug).
- `GET /metrics?subsystem=power&fault_type=NODE_OVERLOAD` (default 1h,
  fully-specified pair) → `200`, all 7 metrics present with
  `datapoints: []` (the fully-specified-pair empty-series rule, verified
  live).
- The same fully-specified query with an explicit 24-hour window → `200`,
  real non-fabricated data: `FaultDetectionCount` sum 12,
  `RecoveryAttemptCount`/`RecoverySuccessCount` sum 13 each,
  `RecoveryFailureCount` empty (0 datapoints, correctly — never
  published), `DetectionLatency` average ~5158ms, `RecoveryLatency`
  average ~7230ms, `UnresolvedFaultCount` sum 1 — all with correct units
  and dimensions.
- A 15-minute-equivalent window (`start`/`end` 900 seconds apart) → `200`,
  confirmed the resolved window is exactly 900 seconds and `period_seconds:
  60`, i.e. the request stayed bounded.
- Unfiltered, 24-hour window → `200`, `series` contained exactly the 6
  real published series (the empty-discard rule correctly omitted the
  never-published `RecoveryFailureCount` series and every one of the
  other 14 dimension pairs with no data), all discovered dimension pairs
  were `(power, NODE_OVERLOAD)` — matching the pre-check above exactly.
- `subsystem=power` alone, 24-hour window → `200`, every returned
  series's `dimensions.Subsystem` was `power`.
- `fault_type=BOGUS` (invalid) → `400 INVALID_FAULT_TYPE`.
- Regression: `GET /nodes?limit=1`, `GET /events?limit=1`,
  `GET /nodes/{known-node-id}/anomalies`, and
  `GET /nodes/{known-node-id}/recoveries` all still return `200` with
  unchanged response shapes.
- CORS, live-verified for `/metrics` specifically: preflight `OPTIONS
  /metrics` with `Origin: http://localhost:5173` → `204` with
  `access-control-allow-origin: http://localhost:5173`,
  `access-control-allow-methods: GET`,
  `access-control-allow-headers: content-type`,
  `access-control-max-age: 300`; the actual `GET /metrics` response also
  carries `access-control-allow-origin: http://localhost:5173`.

**Live frontend validation**: the Vite dev server was started locally
(`http://localhost:5173`) and confirmed to serve the `/analytics` route.
No literal browser/DevTools automation tool is available in this
environment — as disclosed for Steps 40/41/42 — so verification used
`curl` reproducing the exact request and headers a browser's `fetch()`
would send from that origin, confirming a `200` response carrying the
correct CORS header. This is not a substitute for literal visual browser
verification, and is reported as such rather than claimed as full
browser validation.

**Read-only regression check**: exact item counts (via `Scan ... Select
COUNT`), both SQS queue depths, and the most recent Step Functions
execution were captured before deployment and re-checked after all live
testing above (including the IAM investigation/fix cycle):

| Resource | Before | After |
|---|---|---|
| `SentinelGridNodeState` (item count) | 637 | 637 |
| `SentinelGridAnomalyEvents` (item count) | 17 | 17 |
| `SentinelGridRecoveryEvents` (item count) | 16 | 16 |
| `SentinelGridTelemetryQueue` (approx. messages) | 0 | 0 |
| `SentinelGridTelemetryDLQ` (approx. messages) | 0 | 0 |
| Most recent Step Functions execution | `sentinelgrid-remediation-7fc23e8f75e176e6395e9e7cbaca3f49` (SUCCEEDED) | same execution, unchanged — no new execution |

All 13 other CloudFormation stacks' `LastUpdatedTime` and the
`SentinelGridRemediationWorkflow` state machine's ASL definition SHA-256
were also confirmed unchanged before/after both change sets. No
`PutMetricData` call was made at any point (the role was never granted
that permission, and `metrics_service.py`'s own static source-scan test
guarantees it never calls it) — `GET /metrics` and its IAM
troubleshooting produced zero writes of any kind, to DynamoDB, SQS,
Step Functions, or CloudWatch itself.

### What Step 43 does NOT implement

No authentication/authorization (see Step 35/40 above), no
`cloudwatch:ListMetrics` call or permission, no CloudWatch
alarm/dashboard/SNS modification of any kind, no metric publication (this
route is strictly read-only), no percentile (p50/p95/p99) computation, no
MTTR/detection-accuracy/false-positive-rate computation (see
`lambda_functions/knowledge_query/README.md`'s "Metric semantics"
section), no change to `shared/metrics.py` or any metric name/namespace/
unit/dimension/publishing behavior, no Dashboard/Events live-data
changes (only the Analytics page — see `frontend/README.md`), no Lambda
concurrency work, no 500-node scalability test, no frontend deployment,
no literal browser/DevTools automation (see "Live frontend validation"
above).
