# SentinelGrid — Step 47 Security Review & Least-Privilege Audit

**Status:** This is a point-in-time, read-only audit of the deployed SentinelGrid AWS account (`401429382000`, region `ap-south-1`, profile `sentinelgrid`), performed 2026-09-16. It is **not** a certification of security and does **not** claim the system is "secure" or "fully secure." Findings are classified as **VERIFIED**, **PARTIALLY VERIFIED**, **NOT VERIFIED**, or **NOT APPLICABLE** based on direct evidence gathered via read-only AWS API calls and repository inspection. No numerical security score is assigned.

Evidence source: [`scripts/step47_security_audit.py`](../scripts/step47_security_audit.py) → [`docs/step47_security_audit_results.json`](step47_security_audit_results.json) (machine-readable), plus targeted repository greps and manual `describe`/`get`/`list` AWS CLI calls recorded inline below.

---

## 1. Scope and methodology

Read-only. No AWS resource was created, modified, or deleted during this audit. Only `Describe*`/`Get*`/`List*`/`LookUp*` API calls were made (IAM, Lambda, DynamoDB, SQS, IoT, API Gateway v2, CloudWatch, SNS, S3, STS, CloudTrail, Step Functions). No Lambda was invoked, no Step Functions execution was started, no telemetry/metric/SNS data was published.

Scope covers: IAM role least-privilege (10 roles), wildcard permission usage, DynamoDB/SQS/IoT/Lambda/API Gateway/CloudWatch/SNS resource configuration, secrets/credential hygiene across the repository, the S3 Lambda-deployment bucket, CloudTrail/auditability, public network exposure, and the AWS identity used to operate this account.

---

## 2. AWS identity and region

```
Arn:     arn:aws:iam::401429382000:root
Account: 401429382000
Region:  ap-south-1
```

**Finding:** every AWS action in this project — including this audit itself — has been performed as the **AWS account root user**, not an IAM user or role. See §16 for the dedicated finding and remediation.

---

## 3. IAM least-privilege — role by role

All 10 roles use `Service` trust principals (no cross-account or wildcard trust), and none has an attached AWS-managed policy (`AdministratorAccess`, etc.) — every permission is an inline, hand-scoped policy. Full policy documents are in `step47_security_audit_results.json.iam_roles`.

| Role | Trust principal | Permissions summary | Assessment |
|---|---|---|---|
| `SentinelGridAnomalyDetectorExecutionRole` | `lambda.amazonaws.com` | `dynamodb:PutItem` on `AnomalyEvents` only; `dynamodb:UpdateItem` on `NodeState` only; `lambda:InvokeFunction` on `RemediationTrigger` only; `logs:*` scoped to its own log group; `cloudwatch:PutMetricData` with `Resource:*` + namespace condition | VERIFIED least-privilege except the unavoidable metric wildcard (§4) |
| `SentinelGridIoTToDynamoDBRole` | `iot.amazonaws.com` | `dynamodb:PutItem` on `Telemetry` table only | VERIFIED least-privilege |
| `SentinelGridIoTToSqsRole` | `iot.amazonaws.com` | `sqs:SendMessage` on `TelemetryQueue` only | VERIFIED least-privilege |
| `SentinelGridKnowledgeQueryExecutionRole` | `lambda.amazonaws.com` | Read-only `Query`/`Scan`/`GetItem` on 3 tables (Anomaly/NodeState/Recovery), each scoped to its own table ARN; `cloudwatch:GetMetricData` with `Resource:*` (no condition — see §4); `logs:*` scoped to own log group | VERIFIED least-privilege except the GetMetricData wildcard |
| `SentinelGridRemediationExecutorRole` | `lambda.amazonaws.com` | `dynamodb:UpdateItem` on `NodeState`; `PutItem`/`GetItem` on `RecoveryEvents`; `cloudwatch:PutMetricData` (conditioned); `logs:*` own group | VERIFIED least-privilege except metric wildcard |
| `SentinelGridRemediationPlannerRole` | `lambda.amazonaws.com` | Same pattern as Executor | VERIFIED least-privilege except metric wildcard |
| `SentinelGridRemediationTriggerExecutionRole` | `lambda.amazonaws.com` | `states:StartExecution` scoped to exactly `SentinelGridRemediationWorkflow`; `logs:*` own group | VERIFIED least-privilege — no DynamoDB/SQS access at all, correctly minimal for a pure trigger function |
| `SentinelGridRemediationVerifierRole` | `lambda.amazonaws.com` | `dynamodb:UpdateItem` on `NodeState`; `cloudwatch:PutMetricData` (conditioned); `logs:*` own group | VERIFIED least-privilege except metric wildcard |
| `SentinelGridRemediationWorkflowRole` | `states.amazonaws.com` | `lambda:InvokeFunction` scoped to exactly the 3 remediation Lambda ARNs (Planner/Executor/Verifier) — no wildcard, no access to Trigger or AnomalyDetector | VERIFIED least-privilege |
| `SentinelGridSqsConsumerExecutionRole` | `lambda.amazonaws.com` | `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` scoped to `TelemetryQueue` only; `lambda:InvokeFunction` scoped to `AnomalyDetector` only; `logs:*` own group | VERIFIED least-privilege |

**Overall IAM assessment:** every role is scoped to the exact table/queue/function/log-group ARNs it needs, with no role granted access beyond its own function's data path. No role can read/write a table or invoke a function outside its documented responsibility. This is consistent with the least-privilege design already established in prior steps' infrastructure templates.

---

## 4. Wildcard permission audit

5 wildcard (`Resource:"*"`) statements were found, all `cloudwatch:PutMetricData` or `cloudwatch:GetMetricData` — no other action/resource wildcards exist anywhere in the 26 inline policies.

| Role | Action | Condition | Assessment |
|---|---|---|---|
| AnomalyDetectorExecutionRole | `cloudwatch:PutMetricData` | `cloudwatch:namespace = SentinelGrid` | Mitigated — confirmed functionally effective in Step 43's own live-evidence testing |
| RemediationExecutorRole | `cloudwatch:PutMetricData` | `cloudwatch:namespace = SentinelGrid` | Mitigated (same) |
| RemediationPlannerRole | `cloudwatch:PutMetricData` | `cloudwatch:namespace = SentinelGrid` | Mitigated (same) |
| RemediationVerifierRole | `cloudwatch:PutMetricData` | `cloudwatch:namespace = SentinelGrid` | Mitigated (same) |
| KnowledgeQueryExecutionRole | `cloudwatch:GetMetricData` | **none** | **Not mitigated** — `cloudwatch:namespace` does not populate as a condition key for `GetMetricData`'s nested `MetricDataQueries` structure (confirmed in Step 43's investigation); this is a genuine AWS API constraint, not an oversight |

`Resource:"*"` is unavoidable for both actions — CloudWatch's metric APIs do not support resource-level ARN scoping. This should **not** be treated as a standalone vulnerability; the practical risk is that the `KnowledgeQuery` role could call `GetMetricData` against **any** namespace in the account, not just `SentinelGrid` (low impact: read-only, metrics are not sensitive data in this account, and the role has no other cross-namespace capability). Classified **Low** severity (see §17).

---

## 5. DynamoDB security

| Table | Encryption | PITR | Deletion protection | Billing |
|---|---|---|---|---|
| `SentinelGridTelemetry` | Default (AWS-owned key, always-on) | Disabled | Disabled | Pay-per-request |
| `SentinelGridAnomalyEvents` | Default (AWS-owned key, always-on) | Disabled | Disabled | Pay-per-request |
| `SentinelGridRecoveryEvents` | Explicit SSE, AWS-managed KMS key | Disabled | Disabled | Pay-per-request |
| `SentinelGridNodeState` | Explicit SSE, AWS-managed KMS key | Disabled | Disabled | Pay-per-request |

**Encryption note:** `SSEDescription: null` for the first two tables does **not** mean unencrypted — DynamoDB encrypts all tables at rest by default using an AWS-owned key; `SSEDescription` only populates when an *explicit* KMS configuration was set (as it was for the other two tables). All 4 tables are encrypted at rest. VERIFIED.

**PITR / deletion protection:** none of the 4 tables has point-in-time recovery or deletion protection enabled. For an academic/demo system this is a reasonable cost tradeoff, but it means a `DeleteTable` or accidental bulk-delete by the root identity (§16) is unrecoverable. Classified **Medium** (data-loss risk, not a breach risk). No change made — per instructions, nothing was enabled during this audit.

---

## 6. SQS security

| Queue | Encryption | Queue policy | Redrive/DLQ |
|---|---|---|---|
| `SentinelGridTelemetryQueue` | SQS-managed SSE (`SqsManagedSseEnabled: true`) | **Absent** (no resource policy) | DLQ configured: `SentinelGridTelemetryDLQ`, maxReceiveCount 5 |
| `SentinelGridTelemetryDLQ` | SQS-managed SSE | Absent | — |

No queue-level resource policy exists on either queue — access is governed entirely by the IAM identity-based policies on `SentinelGridIoTToSqsRole` (send) and `SentinelGridSqsConsumerExecutionRole` (receive/delete), both scoped to this queue's exact ARN (§3). This is the correct least-privilege pattern: no queue is publicly or cross-account accessible. VERIFIED. No modification made.

---

## 7. IoT Core security

`list_policies`, `list_certificates`, and `list_things` all return **empty** — there is no X.509 certificate / device-identity fleet in this account. This confirms (as established during earlier investigation) that the SentinelGrid simulator authenticates via `boto3.Session(...).client("iot-data", ...)` — the **IoT Data Plane REST API**, authenticated with standard IAM/SigV4 credentials (see [`simulator/aws_publisher.py`](../simulator/aws_publisher.py)) — **not** MQTT + X.509 device certificates.

**Implication:** there is no device-level authorization boundary. Any principal holding IAM credentials for this account (currently the root user — §16) can publish arbitrary telemetry to `sentinelgrid/telemetry`, impersonating any `node_id`. In a real multi-tenant/production IoT deployment this would be a significant gap (no per-device revocation, no device-level least-privilege); for this academic, single-operator, software-only simulation it is a known and accepted architectural simplification, not an oversight. Classified **Medium** for "deployment security limitation," **Not Applicable** for "certification/production readiness." No certificates/policies were rotated or created.

3 IoT Topic Rules were found, all on `sentinelgrid/telemetry`:
- `sentinelgrid_telemetry_to_dynamodb` — enabled
- `sentinelgrid_telemetry_to_sqs` — enabled
- `sentinelgrid_telemetry_to_lambda` — **disabled**, but its target Lambda (`SentinelGridAnomalyDetector`) still carries a live resource-based permission (`lambda:InvokeFunction` for `iot.amazonaws.com`, scoped via `ArnLike` condition to exactly this rule's ARN) — see §9 dormant-permission finding.

---

## 8. Lambda security

All 7 functions run `python3.12` on `arm64`, with `VpcConfig` either absent or empty (no VPC — expected, since none of these functions need to reach a VPC-only resource) and no `FunctionUrlConfig` on any function (no public Lambda Function URLs exist). Environment variables hold only table names and ARNs (`NODE_STATE_TABLE_NAME`, `REMEDIATION_TRIGGER_FUNCTION_ARN`, etc.) — no credential-shaped values. Confirmed via repository grep that no `handler.py`/`remediation/*.py` module hardcodes `profile=` in any boto3 client/resource construction; every module-level singleton (`_event_store = AnomalyEventStore()`, etc.) is a zero-argument constructor, meaning every Lambda relies exclusively on its IAM execution role's temporary credentials, never embedded/static credentials. VERIFIED.

Only 2 of 7 functions carry a resource-based (Lambda) policy — this is expected and correct:

| Function | Resource-based policy | Caller |
|---|---|---|
| `SentinelGridAnomalyDetector` | Yes — `iot.amazonaws.com`, conditioned to the `sentinelgrid_telemetry_to_lambda` rule ARN | IoT Core (AWS-service invocation) |
| `SentinelGridKnowledgeQuery` | Yes — 3 statements, `apigateway.amazonaws.com`, each conditioned to a specific route (`/nodes*`, `/events*`, `/metrics*`) via `AWS:SourceArn` | API Gateway (AWS-service invocation) |
| All other 5 functions | None | Invoked only via same-account IAM identity-based grants (Lambda→Lambda, Step Functions→Lambda) — correct; AWS-service invocations require a resource policy, same-account principal invocations do not |

**Dormant permission finding:** `SentinelGridAnomalyDetector`'s resource-based policy grant for `iot.amazonaws.com` remains active even though the `sentinelgrid_telemetry_to_lambda` rule that would exercise it is currently **disabled** (§7). This is unused attack surface — low severity, since the `ArnLike` condition still scopes it to exactly one rule ARN and the rule can only be re-enabled by an already-privileged IAM principal — but worth cleaning up if the rule is permanently retired. Classified **Low**, in-scope for a future cleanup step, not fixed here (would require `lambda:RemovePermission`, a mutation, outside this audit's read-only scope).

---

## 9. API Gateway security

**The API (`m1uqr1hsq3`, `SentinelGridKnowledgeQueryApi`) is intentionally public and unauthenticated.** All 7 routes (`GET /nodes`, `/nodes/{node_id}`, `/nodes/{node_id}/summary`, `/nodes/{node_id}/anomalies`, `/nodes/{node_id}/recoveries`, `/events`, `/metrics`) have `AuthorizationType: NONE` and `ApiKeyRequired: false`; `authorizers` list is empty. **No authentication was added during this audit** — this is documented as a deployment security limitation, not fixed or hidden.

- **CORS:** restricted to exactly one explicit origin, `http://localhost:5173` (the local frontend dev server) — not `*`. VERIFIED, correctly scoped for the current (non-deployed-frontend, per Step 45's blocked status) state.
- **Rate limiting / throttling:** the `$default` stage has `RouteSettings: {}` and `DefaultRouteSettings: {DetailedMetricsEnabled: false}` — **no `ThrottlingBurstLimit`/`ThrottlingRateLimit` configured at all.** This is a newly-identified gap not documented in any prior step: the public, unauthenticated API has **zero rate limiting**, relying entirely on the AWS account-level default HTTP API throttle (10,000 req/s burst, 5,000 req/s steady-state account-wide) as its only backstop. Classified **Medium** (availability/cost-abuse risk for a public unauthenticated endpoint, not a data-confidentiality risk since all data returned is non-sensitive operational telemetry).
- **Input validation:** all query parameters are strictly validated server-side in [`lambda_functions/knowledge_query/handler.py`](../lambda_functions/knowledge_query/handler.py) — `node_id` length-bounded (`INVALID_NODE_ID`, lines 198-203), `limit` range-checked (`INVALID_LIMIT`, lines 227-229), `order` enum-checked (`INVALID_ORDER`, line 239), `start`/`end` strictly ISO-8601-parsed with a bounded time-range (`TIME_RANGE_TOO_LARGE`, `FUTURE_END_TIMESTAMP`, lines 295-343) — every invalid input path returns a typed `400` via `_ApiError`, never a `500` or silent pass-through. VERIFIED (re-confirmed by direct code read in this audit; originally established in Steps 40-43).

---

## 10. CloudWatch / SNS security

One alarm (`SentinelGrid-UnresolvedFaults-Alarm`, currently `OK`), one dashboard (`SentinelGrid-Observability`), one SNS topic (`SentinelGrid-UnresolvedFaults`, 0 subscriptions, 0 pending). No subscriptions or test messages were added during this audit.

**SNS topic policy:** the `Principal: {"AWS": "*"}` in the topic's policy looks alarming in isolation, but it is paired with `Condition: {StringEquals: {"AWS:SourceOwner": "401429382000"}}` — this is AWS's own standard auto-generated default topic policy, and the condition restricts effective usable access to same-account principals only. **Not** a public-exposure vulnerability. VERIFIED, Informational.

---

## 11. Secrets and credential hygiene

Repository-wide grep for AWS access-key patterns (`AKIA[0-9A-Z]{16}`), private-key headers (`BEGIN ... PRIVATE KEY`), and hardcoded `aws_secret_access_key=`/`password=`/`api_key=` literals found **zero** real matches — every hit was either an existing test's own forbidden-pattern regex (asserting such strings must never appear in a template) or a test fixture intentionally injecting a fake value (`FAKESECRETVALUEFORTESTONLY`) to verify the logging path redacts it. VERIFIED — no embedded secrets found.

`.env`/`.env.*` are covered by both the root and `frontend/.gitignore`; `frontend/.env.local` (present locally, not git-tracked) contains only `VITE_API_BASE_URL=https://m1uqr1hsq3.execute-api.ap-south-1.amazonaws.com` — a public API endpoint URL, not a secret. VERIFIED.

---

## 12. S3 deployment-bucket security

Bucket: `sentinelgrid-lambda-deploy-401429382000`.

| Control | Status |
|---|---|
| Public Access Block | All 4 settings `true` (BlockPublicAcls/IgnorePublicAcls/BlockPublicPolicy/RestrictPublicBuckets) |
| Bucket policy | Absent (`NoSuchBucketPolicy`) — access governed by IAM only |
| Encryption | `AES256` (SSE-S3), `BucketKeyEnabled: false`, `SSE-C` explicitly blocked |
| Versioning | Enabled |
| Object ownership | `BucketOwnerEnforced` (ACLs disabled account-wide for this bucket) |

VERIFIED well-configured — fully private, encrypted, versioned, no ACL-based access path. No old versions deleted, no settings modified.

---

## 13. CloudTrail / auditability

`describe-trails` returns an **empty list** — no explicit Trail resource exists (no durable S3-delivered trail, no long-term retention, no data-event logging). `list-event-data-stores` is also empty. However, `lookup-events` (default Event History) returns results, confirming the account-wide, always-on, 90-day CloudTrail Event History **is** active (this requires no explicit resource — it's an AWS-account-level default).

**Assessment:** management-event auditability exists but is bounded to a 90-day rolling window with no durable export. Classified **Medium** (limits post-incident forensics beyond 90 days; no data-event logging for S3/Lambda invocations). No Trail was created during this audit, per instructions.

---

## 14. Public network exposure

| Resource | Public? | Notes |
|---|---|---|
| API Gateway (`m1uqr1hsq3`) | **Yes, intentionally** | Public HTTP API, unauthenticated (§9) |
| S3 deployment bucket | No | Public Access Block fully enabled (§12) |
| Lambda Function URLs | None configured | Confirmed via `get_function_url_config` on all 7 functions |
| DynamoDB tables | No | No public endpoint; API-only via IAM |
| SQS queues | No | No queue policy; IAM-only (§6) |
| CloudFront / S3 website (frontend) | **Not deployed** | Step 45 remains BLOCKED by AWS account-level CloudFront verification; no public frontend exists |

The only intentionally public surface is the Knowledge Query API, and it is documented above as unauthenticated with no rate limiting.

---

## 15. Root-account usage finding

**This is the most significant finding of this audit.** `aws sts get-caller-identity` resolves to `arn:aws:iam::401429382000:root` — every AWS action across this project's entire lifetime (all prior steps, and this audit itself) has been performed as the **AWS account root user**, not a scoped IAM user or role.

**Risk:** the root identity has unrestricted, unscopeable access to the entire account — billing, IAM, every resource, account settings — with no IAM policy able to limit it. Any credential compromise at this level compromises the whole account, not just SentinelGrid's resources. This is a materially different risk profile from every other finding in this report, which all assume an IAM-role-scoped caller.

**Remediation (recommended, NOT performed):** create a dedicated IAM user or role (e.g. `SentinelGridOperator`) with an explicit least-privilege policy covering exactly the actions this project's tooling needs (CloudFormation deploy, `iot-data:Publish`, the read-only `Describe`/`Get`/`List` calls this audit itself makes, etc.), and stop using root for day-to-day operations. Per explicit instruction, **no change to account credentials or root settings was made** in this step — this is a documented risk and recommendation only.

Classified **Critical** in §17.

---

## 16. Step Functions ASL integrity verification

Three different SHA-256 hashes were observed for what is the **same underlying content**, fully explained (no unauthorized drift found):

1. **Local repository template** (`step_functions/remediation_workflow.json`): `shasum -a 256` → `0135027f4b0c98a72964c63494097e26e37eafe0c912a5f93355f87f855d1d3e` — matches the task's stated expected value exactly. This file is, by its own documented `Comment` field, a **pre-deployment template** containing `${...}` CloudFormation `Fn::Sub` placeholders and is explicitly "not deployed, not registered with AWS Step Functions."
2. **Live deployed definition, captured via `aws stepfunctions describe-state-machine --output text | shasum`**: differs from (1) only in 3 `Resource` fields (real ARNs substituted for the `${...}` placeholders, as designed) plus the `Comment` text — confirmed via a full parsed-JSON diff.
3. **Live deployed definition, captured via boto3 directly**: differed from (2) only by a trailing newline that the CLI's `--output text` appends and boto3's raw string does not — a **capture-tool artifact**, not a content difference.

After `.strip()`-normalizing before hashing (now implemented in `scripts/step47_security_audit.py::audit_step_functions`), **all three capture methods converge** to the same value: `b861896cbc7ed4a75de1ee19aabf515907dab8e1668a3e0bcb9fb4f6ea09dc7e`. This was independently re-verified in this session.

**Conclusion:** the local template is unchanged (matches the task's expected hash exactly); the live deployed workflow is unchanged (stable across every verification this session); neither hash discrepancy represents unauthorized modification. The task's stated expected value (`0135027f...`) is confirmed to be the **template's** hash, not the deployed artifact's hash — an expected and designed difference, not a security finding.

---

## 17. Findings classification table

| ID | Area | Finding | Evidence | Severity | Status | Recommended remediation | In scope for this project |
|---|---|---|---|---|---|---|---|
| F1 | Identity | All AWS operations performed as account root, not a scoped IAM identity | `sts:get-caller-identity` → `:root` | **Critical** | VERIFIED | Create a dedicated least-privilege IAM user/role for project operations; stop using root | Recommended, not performed (explicit instruction not to change account credentials/root settings) |
| F2 | API Gateway | Public Knowledge Query API has no rate limiting/throttling configured on `$default` stage | `RouteSettings: {}`, no `ThrottlingBurstLimit`/`ThrottlingRateLimit` | Medium | VERIFIED | Add stage-level throttle limits via the existing CloudFormation template | Yes, low-risk, future step |
| F3 | API Gateway | API is public and unauthenticated by design | `AuthorizationType: NONE` on all 7 routes, no authorizers | Medium | VERIFIED (documented limitation, not a bug) | Add an authorizer (API key, IAM, or Cognito) if the API is ever exposed beyond a demo/academic context | Explicitly out of scope for this step |
| F4 | DynamoDB | No PITR or deletion protection on any of 4 tables | `describe_continuous_backups` / `describe_table` | Medium | VERIFIED | Enable PITR at minimum on `NodeState`/`RecoveryEvents` before any production use | Recommended, not performed |
| F5 | CloudTrail | No durable Trail resource; only 90-day default Event History | `describe_trails` empty; `lookup_events` returns results | Medium | VERIFIED | Create an explicit S3-backed Trail for durable, longer-retention audit logs | Recommended, not performed (explicit instruction not to create CloudTrail resources) |
| F6 | IoT Core | No device-level authentication boundary; simulator publishes via IAM/SigV4, not X.509/MQTT | `list_policies`/`list_certificates`/`list_things` all empty; `simulator/aws_publisher.py` uses `iot-data` REST client | Medium | VERIFIED | Acceptable for a single-operator academic simulation; would need device certs + per-device IoT policies for a real multi-tenant deployment | Documented; out of scope to redesign |
| F7 | IAM wildcard | `cloudwatch:GetMetricData` granted with `Resource:"*"` and no condition | `KnowledgeQueryExecutionRole` inline policy | Low | VERIFIED | Accept as an AWS API constraint (GetMetricData has no resource-level ARN support); no fix available at the IAM layer | Not applicable — AWS limitation |
| F8 | IAM wildcard | `cloudwatch:PutMetricData` granted with `Resource:"*"`, mitigated via namespace condition | 4 roles, each with `cloudwatch:namespace=SentinelGrid` condition | Informational | VERIFIED (mitigated) | None needed | N/A |
| F9 | Lambda | Dormant resource-based permission for a disabled IoT rule | `SentinelGridAnomalyDetector` resource policy references `sentinelgrid_telemetry_to_lambda` (disabled) | Low | VERIFIED | Remove the permission (`lambda:RemovePermission`) if the rule is permanently retired | Yes, future cleanup step (mutation, not performed here) |
| F10 | DynamoDB | 2 of 4 tables show `SSEDescription: null` | `describe_table` | Informational | VERIFIED (both tables ARE encrypted via default AWS-owned key; only explicit-KMS config populates this field) | None needed — not a real gap | N/A |
| F11 | SNS | Default topic policy has `Principal: {"AWS":"*"}` | `get_topic_attributes` | Informational | VERIFIED (mitigated by `SourceOwner` condition — AWS's own standard default policy) | None needed | N/A |
| F12 | Secrets | No hardcoded credentials found anywhere in the repository | Repo-wide grep, all hits are test fixtures/assertions | — | VERIFIED | None needed | N/A |
| F13 | S3 | Deployment bucket fully private, encrypted, versioned | `get_public_access_block`, `get_bucket_encryption`, `get_bucket_versioning` | — | VERIFIED (good posture) | None needed | N/A |
| F14 | SQS | No queue-level resource policy on either queue; access via IAM only | `get_queue_attributes` (no `Policy` attribute) | — | VERIFIED (good posture) | None needed | N/A |

No finding in this table was fabricated or inferred without direct evidence; where evidence was insufficient to reach a conclusion, the row is marked NOT VERIFIED / NOT APPLICABLE rather than guessed (none of the 14 findings above required that marking — all had direct evidence either way).

---

## 18. Explicit limitations — not verified in this audit

- **Data-plane CloudTrail events** (S3 object-level, Lambda invocation-level) were not checked — only the fact that *some* management-event history exists via `lookup_events` was confirmed. Whether specific past actions (e.g., a specific `PutItem` call) are individually traceable was NOT VERIFIED.
- **Actual exploitability** of the unauthenticated public API (F3) — e.g., whether the account-level default HTTP API throttle is sufficient to prevent real-world abuse — was NOT VERIFIED; only the absence of stage-level throttling configuration was confirmed.
- **Historical IAM policy changes** (whether any role was ever broader in the past and later tightened) were NOT VERIFIED — this audit reflects only the current, point-in-time state.
- **VPC/network-layer controls** were NOT APPLICABLE — no SentinelGrid Lambda uses a VPC, so there are no security groups/NACLs to audit.
- **KMS key policy** for the AWS-managed key used by `NodeState`/`RecoveryEvents` (`arn:aws:kms:ap-south-1:401429382000:key/4ec539b2-...`) was NOT VERIFIED in detail — only that SSE is enabled with this key was confirmed; the key's own IAM/key policy was not separately audited.
- **CloudFront/S3 frontend hosting security** is NOT APPLICABLE — Step 45 remains blocked and no frontend is deployed; there is nothing to audit there.

---

## 19. Recommended next engineering step

If a future step is authorized to make changes (not part of this read-only audit), the highest-value, lowest-risk next action would be: **create a dedicated least-privilege IAM user or role for day-to-day project operations (F1)** and stop operating as account root — every other finding in this report assumes an IAM-scoped caller as the baseline threat model, and F1 is the one finding that undermines all of them simultaneously. A close second would be adding stage-level throttling to the public API (F2), since it requires only a CloudFormation parameter change to the existing `sentinelgrid-knowledge-query` stack.

This audit does not recommend or perform any such change now.
