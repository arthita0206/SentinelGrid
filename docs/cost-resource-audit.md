# SentinelGrid — Step 49: Cost & Resource Audit

**Status:** Read-only audit of the deployed SentinelGrid AWS account (`401429382000`, region `ap-south-1`, profile `sentinelgrid`, identity `arn:aws:iam::401429382000:root`), performed 2026-09-18. No AWS resource was created, modified, or deleted. No numerical cost/security score is assigned; no dollar figure is asserted beyond what AWS's own Cost Explorer API returned.

Evidence source: [`scripts/step49_cost_resource_audit.py`](../scripts/step49_cost_resource_audit.py) → [`docs/step49_cost_resource_audit_results.json`](step49_cost_resource_audit_results.json), plus local repository inspection of `infrastructure/*.yaml`/`*.sh`.

---

## 1. CloudFormation stack inventory

`describe_stacks` was queried for all 17 known stack names, and `list_stacks` (no status filter) was queried to catch anything unexpected.

| Stack | Status | Created | Classification |
|---|---|---|---|
| `sentinelgrid-telemetry-storage` | CREATE_COMPLETE | 2026-09-05 | Current |
| `sentinelgrid-anomaly-detector` | UPDATE_COMPLETE | 2026-09-05 | Current |
| `sentinelgrid-anomaly-detection-rule` | UPDATE_COMPLETE | 2026-09-05 | Current |
| `sentinelgrid-anomaly-events-storage` | UPDATE_COMPLETE | 2026-09-13 | Current |
| `sentinelgrid-remediation-lambdas` | UPDATE_COMPLETE | 2026-09-14 | Current |
| `sentinelgrid-remediation-workflow` | CREATE_COMPLETE | 2026-09-14 | Current (one earlier DELETE_COMPLETE attempt from 2026-09-13 — expected recreate, not a duplicate) |
| `sentinelgrid-remediation-trigger` | CREATE_COMPLETE | 2026-09-14 | Current |
| `sentinelgrid-telemetry-sqs-queue` | UPDATE_COMPLETE | 2026-09-14 | Current |
| **`sentinelgrid-telemetry-kinesis-stream`** | **ROLLBACK_COMPLETE** | 2026-09-14 | **Legacy/abandoned — see §1.1** |
| `sentinelgrid-sqs-consumer` | CREATE_COMPLETE | 2026-09-14 | Current (one earlier DELETE_COMPLETE attempt same day — expected recreate) |
| `sentinelgrid-observability-dashboard` | CREATE_COMPLETE | 2026-09-14 | Current |
| `sentinelgrid-observability-alerting` | CREATE_COMPLETE | 2026-09-14 | Current |
| `sentinelgrid-recovery-events-storage` | UPDATE_COMPLETE | 2026-09-14 | Current |
| `sentinelgrid-node-state-storage` | CREATE_COMPLETE | 2026-09-14 | Current |
| `sentinelgrid-knowledge-query` | UPDATE_COMPLETE | 2026-09-14 | Current |
| `sentinelgrid-frontend-hosting` | **Does not exist** (`describe_stacks` returns not-found) | — | Two DELETE_COMPLETE records exist in `list_stacks` history (2026-09-15, 2026-09-16) — both from the two Step 45 attempts that were cleanly rolled back and deleted. No live resources. |
| `sentinelgrid-operator-identity` | CREATE_COMPLETE | 2026-09-16 | Current (Step 48) |

No unexpected/unknown stack names were found in the full `list_stacks` output — every stack present belongs to the known SentinelGrid stack set.

### 1.1 The Kinesis stack — investigated, not a live cost risk

`sentinelgrid-telemetry-kinesis-stream` is `ROLLBACK_COMPLETE`, with one resource (`TelemetryStream`, `AWS::Kinesis::Stream`) showing status `DELETE_SKIPPED`. Taken alone, `DELETE_SKIPPED` can indicate a resource that survived a rollback and is still billing. This was investigated directly rather than assumed:

```
aws kinesis list-streams --profile sentinelgrid --region ap-south-1
-> SubscriptionRequiredException: The AWS Access Key Id needs a
   subscription for the service
```

This account is **not subscribed to Amazon Kinesis at all** — every Kinesis API call, including the harmless read-only `ListStreams`, is rejected before it even reaches account-specific data. This is strong evidence that the original `CREATE` of this stack failed immediately (most likely for the same subscription reason) before AWS ever provisioned a billable stream, and `DELETE_SKIPPED` reflects "nothing to delete," not "failed to delete an existing resource." This is consistent with the project's own architecture, which uses SQS (not Kinesis) for telemetry ingestion — this stack was an abandoned early design alternative, never part of the live data plane.

**Conclusion:** high confidence of no ongoing cost from this stack. Classified as a documentation-only cleanup candidate (§7), not a cost risk requiring urgent attention.

---

## 2. Complete deployed resource inventory (as configured)

| Category | Resources |
|---|---|
| DynamoDB | 4 tables (Telemetry, AnomalyEvents, RecoveryEvents, NodeState), all `PAY_PER_REQUEST` |
| SQS | 2 queues (TelemetryQueue + TelemetryDLQ) |
| Lambda | 7 functions, all `python3.12`/`arm64`/128MB, no reserved/provisioned concurrency, no Function URLs |
| IoT Core | 3 topic rules (2 enabled: to-DynamoDB, to-SQS; 1 disabled: to-Lambda) |
| Step Functions | 1 state machine (`SentinelGridRemediationWorkflow`), `ACTIVE`, logging OFF, tracing disabled |
| API Gateway | 1 HTTP API (`m1uqr1hsq3`), 7 routes, 1 stage (`$default`), no stage-level throttling |
| CloudWatch | 1 alarm, 1 dashboard, 1 custom namespace (6 metric names, 11 metric/dimension series), 7 Lambda log groups |
| SNS | 1 topic, 0 subscriptions |
| S3 | 1 bucket (`sentinelgrid-lambda-deploy-401429382000`), versioning enabled |
| IAM | 10 service-execution roles + 1 operator role (Step 48), all inline-policy-only, no attached managed policies |
| CloudFront | Not deployed (Step 45 remains blocked; not retried this step, per instruction) |

Lambda account concurrency quota: **400** (confirmed live via `get_account_settings`; unchanged since Step 38).

---

## 3. Lambda cost profile

| Function | Memory | Timeout | Code size | Reserved concurrency | Function URL |
|---|---|---|---|---|---|
| AnomalyDetector | 128 MB | 10s | 26.1 KB | none | none |
| SqsTelemetryConsumer | 128 MB | 60s | 6.0 KB | none | none |
| RemediationTrigger | 128 MB | 10s | 19.3 KB | none | none |
| RemediationPlanner | 128 MB | 10s | 49.2 KB | none | none |
| RemediationExecutor | 128 MB | 10s | 49.2 KB | none | none |
| RemediationVerifier | 128 MB | 10s | 49.2 KB | none | none |
| KnowledgeQuery | 128 MB | 10s | 25.0 KB | none | none |

**Cost classification: (1) No/very-low ongoing cost under idle configuration; (2) usage-dependent when exercised.** All 7 functions are the smallest practical memory tier (128 MB, AWS Lambda's minimum), no reserved/provisioned concurrency (so no idle-capacity charge), no Function URLs (so no direct-invoke public surface billing separately from API Gateway). Lambda's AWS Free Tier (1M requests + 400,000 GB-seconds/month, indefinite, not time-limited) comfortably covers this project's demonstrated workload (Step 38's 500-node/6,000-telemetry test was the largest single run). Cost scales with invocation count × duration if workload grows substantially (e.g., sustained large-scale simulation), but nothing here is misconfigured for cost.

---

## 4. DynamoDB cost profile

| Table | Billing mode | Item count | Size | PITR |
|---|---|---|---|---|
| SentinelGridTelemetry | PAY_PER_REQUEST | 12,586 | 1.9 MB | Disabled |
| SentinelGridAnomalyEvents | PAY_PER_REQUEST | 21 | 5.6 KB | Disabled |
| SentinelGridRecoveryEvents | PAY_PER_REQUEST | 21 | 7.3 KB | Disabled |
| SentinelGridNodeState | PAY_PER_REQUEST | 1,142 | 297 KB | Disabled |

*`ItemCount`/`TableSizeBytes` are DynamoDB's own periodic estimates (updated roughly every 6 hours by AWS), not a live `COUNT` — this is an AWS API characteristic, not an audit limitation.*

**Cost classification: (1) No/very-low ongoing cost.** All 4 tables use `PAY_PER_REQUEST` (on-demand) billing — no provisioned read/write capacity units accrue idle cost. Combined storage is ~2.2 MB, trivially within DynamoDB's free tier (25 GB storage). No PITR means no continuous-backup storage charge either (a cost-saving side effect of a configuration also flagged in Step 47 as a data-durability risk, F4 — the two considerations pull in opposite directions and both are documented, not resolved, here). Cost scales with read/write request volume and total storage if the dataset grows substantially.

---

## 5. SQS cost profile

| Queue | Messages | Not visible | Retention | Encryption |
|---|---|---|---|---|
| SentinelGridTelemetryQueue | 0 | 0 | 4 days | SQS-managed SSE |
| SentinelGridTelemetryDLQ | 0 | 0 | 14 days | SQS-managed SSE |

**Cost classification: (1) No/very-low ongoing cost.** Both queues are empty (consistent with Step 38's confirmed full-drain behavior). SQS's free tier (1M requests/month) comfortably covers demonstrated usage; cost scales with message volume if sustained high-throughput testing resumes.

---

## 6. IoT cost profile

3 topic rules on `sentinelgrid/telemetry`: `sentinelgrid_telemetry_to_dynamodb` (enabled), `sentinelgrid_telemetry_to_sqs` (enabled), `sentinelgrid_telemetry_to_lambda` (**disabled**). The disabled rule and its associated dormant Lambda resource-based permission (documented in Step 47, F9) were re-confirmed present — this is a zero-cost, low-severity unused-configuration item, not a billing concern (disabled IoT rules and unused Lambda permissions do not themselves incur charges).

**Cost classification: (1) No/very-low ongoing cost under idle configuration; (2) usage-dependent.** IoT Core pricing is per-message and per-rule-action; with 0 active telemetry publishing at rest, current cost is negligible. Cost scales directly with simulator message volume during test runs.

---

## 7. Step Functions cost profile

State machine `SentinelGridRemediationWorkflow`: `ACTIVE`, Standard type (implied by `describe_state_machine` fields), **logging OFF** (`level: OFF`), **tracing disabled**. 26 executions found via `list_executions` (capped at 1,000 — well under the cap, so this is a complete count), all `SUCCEEDED`, consistent with this project's controlled-test history (Steps 27, 38, 46, etc.) — no unexpected/unauthorized executions.

**Cost classification: (1) No/very-low ongoing cost.** Step Functions Standard workflows are billed per state transition; 26 executions × 3 states (Plan/Execute/Verify) ≈ 78 transitions total across this project's entire history — trivially within the free tier (4,000 free transitions/month). Logging being OFF avoids any CloudWatch Logs charge from execution history. No active/running executions were found.

---

## 8. API Gateway cost profile

HTTP API `m1uqr1hsq3`, 7 routes, 1 stage (`$default`, auto-deploy), `DetailedMetricsEnabled: false`, no stage-level throttling configured (previously documented as security finding F2 in Step 47 — also relevant here as an *uncapped* request-volume cost exposure, not just a security gap).

**Cost classification: (2) usage-dependent; (4) cost risk requiring attention (indirect).** HTTP APIs are billed per request; with no throttling, request volume — and therefore cost — is unbounded if the endpoint receives unexpected/abusive traffic (relevant since the API is public and unauthenticated, per Step 47 F3). `DetailedMetricsEnabled: false` avoids extra per-metric CloudWatch charges. This is the same underlying gap as Step 47's F2, viewed here through a cost rather than security lens.

---

## 9. CloudWatch / logging / metrics cost profile

- **1 alarm** (`SentinelGrid-UnresolvedFaults-Alarm`), **1 dashboard** (`SentinelGrid-Observability`) — both trivial fixed monthly costs (CloudWatch's free tier covers 10 alarms and 3 dashboard-equivalents; this project uses far less).
- **Custom metric namespace `SentinelGrid`**: 6 metric names, 11 distinct metric/dimension series (`list_metrics`). CloudWatch's free tier covers the first 10 custom metrics; this project is marginally at/near that boundary — worth monitoring if additional metric dimensions are added.
- **7 Lambda log groups, ALL with retention set to "Never Expire"** (`retentionInDays` absent from every one) — confirmed live. Combined stored size: ≈5.06 MB (AnomalyDetector 3.34 MB, SqsTelemetryConsumer 1.66 MB, the 5 others 10–21 KB each). This is currently trivial (CloudWatch Logs free tier: 5 GB ingestion + storage), but **will grow indefinitely** with no automatic pruning, since no template defines an explicit `AWS::Logs::LogGroup` resource with a retention policy — Lambda auto-creates each log group with the AWS default (never expire) the first time the function runs. **Classified as an ongoing cost/storage consideration per the task's explicit instruction to flag "Never Expire" retention.**

**Cost classification: (1) No/very-low ongoing cost today; (3) potentially significant if log volume grows substantially over a long project lifetime without ever being pruned.**

---

## 10. SNS cost profile

1 topic (`SentinelGrid-UnresolvedFaults`), 0 subscriptions.

**Cost classification: (1) No/very-low ongoing cost.** SNS is billed per-notification-delivered; with zero subscriptions, no deliveries are possible from this topic regardless of how many times the alarm fires.

---

## 11. S3 cost profile

Bucket `sentinelgrid-lambda-deploy-401429382000`, region `ap-south-1`, versioning **Enabled**, **no lifecycle configuration** (`NoSuchLifecycleConfiguration`).

| Metric | Value |
|---|---|
| Current object versions | 9 |
| **Noncurrent object versions** | **24** |
| Delete markers | 0 |
| Total size (all versions combined) | 741,583 bytes (~724 KB) |
| Incomplete multipart uploads | 0 |

**Cost classification: (1) No/very-low ongoing cost today given the tiny total size (~724 KB, trivially within S3's free tier); (3) potentially significant if left unmanaged over a much longer project lifetime.** The task specifically flagged S3 versioning as worth attention because Lambda deployment artifacts have been repeatedly replaced during development — confirmed: 24 noncurrent versions exist against only 9 current ones (a ~2.7:1 ratio), and with **no lifecycle rule** to expire noncurrent versions automatically, every past deployment package remains stored (and billed) forever. At current total size this is cost-negligible, but the pattern (no expiration policy, versioning always on) means the ratio will keep growing with every future redeploy.

---

## 12. IAM / resource-management observations

11 SentinelGrid-prefixed roles total (10 service-execution roles + `SentinelGridOperatorRole` from Step 48), all inline-policy-only (0 attached managed policies each) — consistent with Step 47's least-privilege audit. IAM roles themselves carry no direct cost (IAM is a free service); this section is included for completeness per the task's Phase 3J request, not because it drives cost. `SentinelGridOperatorRole` (created 2026-09-16) remains unusable by any human identity (Step 48 — root cannot assume roles; Phase 6 of that step was left incomplete pending an account-owner decision) — re-confirmed unchanged, not modified this step.

---

## 13. Actual billing data

**AWS Cost Explorer was accessible** to the current (root) identity — queried via `ce:GetCostAndUsage`, grouped by service, for two periods:

| Period | Total UnblendedCost (summed across all reported services) |
|---|---|
| 2026-08-01 to 2026-09-01 (complete prior month) | **$0.00** (5 services listed, all exactly $0 or a negligible negative rounding artifact ≈ -$0.0000000124) |
| 2026-09-01 to 2026-09-18 (current month-to-date) | **$0.00** (18 services listed — including AWS Lambda, AWS IoT, AWS CloudFormation, AWS Key Management Service — every one exactly $0 or a negligible negative rounding artifact) |

This is **real, retrieved billing data**, not an estimate or assumption — the account genuinely shows $0.00 unblended cost for every AWS service across the full prior month and the current month-to-date. The tiny negative amounts (e.g., -$0.0000001342 for "AWS Data Transfer") are consistent with AWS's own sub-cent rounding/credit artifacts, not real charges.

---

## 14. Current accumulated data / test-data footprint

| Data set | Count | Size | Can it grow indefinitely? |
|---|---|---|---|
| SentinelGridTelemetry | 12,586 items | 1.9 MB | **Yes** — every simulator run (Steps 34, 36, 37, 38, 46) appends more; nothing prunes it |
| SentinelGridAnomalyEvents | 21 items | 5.6 KB | Yes, same pattern, much smaller volume (only anomalous conditions persist here) |
| SentinelGridRecoveryEvents | 21 items | 7.3 KB | Yes, same pattern |
| SentinelGridNodeState | 1,142 items | 297 KB | Partially self-limiting — one item per unique `node_id` (overwritten, not appended), but distinct node IDs across all historical test runs (each using unique infixes, per the project's own testing convention) keep accumulating |
| S3 deployment artifacts (all versions) | 33 total (9 current + 24 noncurrent) | 724 KB | **Yes** — no lifecycle rule; every future redeploy adds more noncurrent versions forever |
| CloudWatch Lambda logs | 7 log groups | ~5.06 MB combined | **Yes** — "Never Expire" retention on every log group |
| CloudWatch custom metric series | 11 series, 6 metric names | N/A (metrics aren't directly billed by volume, only by unique metric count) | Bounded by the fixed set of metric names the code emits — will not grow further unless new metric names/dimensions are added |

No historical data was deleted or modified during this audit, per explicit instruction.

---

## 15. Cost drivers and risks

| Driver | Classification | Notes |
|---|---|---|
| Lambda invocations/duration | (2) Usage-dependent | Free tier comfortably covers demonstrated workload |
| DynamoDB reads/writes/storage | (1)/(2) | PAY_PER_REQUEST, no idle cost; scales with usage |
| SQS requests | (1)/(2) | Free tier covers demonstrated usage |
| IoT messages/rules | (1)/(2) | Negligible at rest; scales with publish volume |
| Step Functions transitions | (1) | 26 executions total, far under free tier |
| API Gateway requests | **(3) potentially significant if workload increases** — **no throttling configured** | Public + unauthenticated + unthrottled = unbounded request-volume exposure |
| CloudWatch logs (Never Expire) | **(3) potentially significant over a long project lifetime** | No pruning; currently trivial (~5 MB) |
| CloudWatch custom metrics | (1), near free-tier boundary | 11 series against a 10-metric free allotment — worth monitoring, not urgent |
| CloudWatch dashboard/alarm | (1) | 1 each, well within free tier |
| SNS | (1) | 0 subscriptions, no deliveries possible |
| S3 storage/versioning | **(3) potentially significant over a long project lifetime** — **no lifecycle rule** | 24 noncurrent versions and growing; currently trivial size |
| CloudFormation | (1) | No direct charge for the service itself |
| Abandoned Kinesis stack | (1), verified not a live resource | See §1.1 |

**No cost driver in this account is currently classified (4) "cost risk requiring attention" in absolute dollar terms** — actual billing data shows $0.00. The (3)-classified items (API Gateway throttling, log retention, S3 lifecycle) are risks to the *trajectory* of cost over time, not current cost.

---

## 16. Cleanup candidates — DOCUMENT ONLY, NO DELETION

| Resource | Why it appears unused/obsolete | Confidence | Risk of deleting | Retain for demo? | Recommended timing | Category |
|---|---|---|---|---|---|---|
| `sentinelgrid-telemetry-kinesis-stream` CFN stack record (ROLLBACK_COMPLETE) | Abandoned early design (Kinesis never subscribed in this account); no live resource confirmed (§1.1) | High | Low — stack has no active resources; deleting the stack record removes only the failed-deployment history | No — not part of the live architecture | Anytime after this audit; not urgent since it costs nothing | CLEANUP CANDIDATE |
| Two `sentinelgrid-frontend-hosting` DELETE_COMPLETE stack history entries | Already fully deleted (Step 45's two blocked attempts); `list_stacks` retains DELETE_COMPLETE history for a limited time automatically, no action needed | High | None — already deleted, this is just history AWS itself will eventually prune | N/A | No action needed — AWS auto-expires this | RETAIN (informational only, self-resolving) |
| `sentinelgrid_telemetry_to_lambda` IoT rule (disabled) + its dormant Lambda resource-based permission | Rule intentionally disabled; permission unused (documented Step 47 F9) | High | Low if rule is confirmed permanently retired — but check whether any future step intends to re-enable it first | Keep for now — deleting requires confirming this path is permanently abandoned, not scoped to this step | Defer to a dedicated cleanup step with explicit confirmation it's permanently retired | REVIEW |
| 24 noncurrent S3 object versions (deployment artifacts) | Superseded by newer Lambda package uploads; no lifecycle rule to auto-expire them | High (superseded) / Medium (whether ALL are safe — the most recent noncurrent version of each key might still be useful as a rollback point) | Medium — deleting the wrong noncurrent version could remove a genuine rollback point; requires per-key review, not a blanket delete | Retain at least the most recent 1-2 noncurrent versions per key for rollback safety | Adding a lifecycle rule (e.g., expire noncurrent versions after 30-90 days) is lower-risk than manual deletion — recommend that approach in a future step | REVIEW |
| CloudWatch Lambda log groups with "Never Expire" retention | No retention policy was ever configured (default AWS behavior, not a deliberate choice) | High (config gap confirmed) | Low — setting retention going forward doesn't delete existing logs; only bounds future growth | N/A — this is a configuration gap, not a resource to delete | Low-risk future step: set an explicit retention period (e.g., 30-90 days) via `PutRetentionPolicy` on each of the 7 log groups | REVIEW |
| Historical DynamoDB test data (12,586 telemetry items, 1,142 NodeState items, etc.) | Genuine accumulated evidence from Steps 34-46's controlled tests | N/A — not obsolete, this is real project history | High if deleted carelessly — this data supports prior steps' documented findings | **Yes — explicitly retain for the final demonstration and as evidence backing this project's prior test reports** | Do not delete; if ever pruned, do so only after the project is fully concluded and prior reports no longer need to reference live data | RETAIN |
| `SentinelGridOperatorRole` (Step 48) | Not obsolete — a deliberately-created, currently-unusable-but-inert least-privilege role awaiting a human-authentication decision | N/A | N/A — not a deletion candidate | Yes, retain pending Step 48's Phase 6 decision | No action this step | RETAIN |

No resource was labeled "safe to delete" without the dependency/verification reasoning shown above, per the task's explicit instruction.

---

## 17. Final demo required resources

| Component | Demo Required | Optional/Historical |
|---|---|---|
| IoT Core rules (to-SQS, to-DynamoDB) | ✅ Required | |
| SQS queue + DLQ | ✅ Required | |
| SqsTelemetryConsumer, AnomalyDetector Lambdas | ✅ Required | |
| DynamoDB: Telemetry, AnomalyEvents, NodeState | ✅ Required | |
| RemediationTrigger, Step Functions workflow, Planner/Executor/Verifier | ✅ Required | |
| DynamoDB: RecoveryEvents | ✅ Required | |
| CloudWatch dashboard + alarm + custom metrics | ✅ Required (observability is part of the architecture's own value proposition) | |
| Knowledge Query API (API Gateway + Lambda) | ✅ Required | |
| SNS topic | ✅ Required (alarm action target, even with 0 subscriptions) | |
| S3 deployment bucket | ✅ Required (Lambda packages are deployed from it) | |
| Frontend / CloudFront | | Optional — blocked (Step 45); demo can proceed via API calls / existing dashboards without it |
| `sentinelgrid_telemetry_to_lambda` disabled IoT rule | | Historical/unused — not part of the live demo path |
| `sentinelgrid-telemetry-kinesis-stream` failed stack | | Historical — abandoned design alternative |
| Accumulated historical test data (12,586+ telemetry items, etc.) | | Historical — supports prior reports; not required for a fresh demo run, but should not be deleted |
| `SentinelGridOperatorRole` | | Historical/in-progress — not part of the runtime data plane; a Step 48 in-progress security improvement |

Every "Demo Required" resource listed above is already deployed and, per this audit, correctly configured and low-cost.

---

## 18. NFR-9 assessment (low-cost / student-account constraint)

**DEMONSTRATED**, with explicit evidence and explicit caveats:

- **Evidence for DEMONSTRATED:** Real AWS Cost Explorer data (§13) shows **$0.00 unblended cost** across every AWS service for the complete prior month and the current month-to-date — this is actual observed billing, not an assumption or an estimate. Every resource category was independently checked and found in a low-cost configuration: DynamoDB is 100% on-demand (no provisioned capacity), Lambda uses minimum memory with no reserved/provisioned concurrency, SQS/SNS/Step Functions/CloudWatch usage is all trivially within AWS free-tier thresholds given the demonstrated workload, and no orphaned billable resource was found (the one apparent candidate, the Kinesis stack, was investigated and shown with high confidence to have never actually provisioned a billable resource).
- **Why not simply "PASS" on architecture alone (per the task's explicit instruction not to default to that):** the assessment above is grounded in *actual billing data and actual configuration*, not an assumption that "serverless = free." Two real, config-level cost risks were found that are **not yet demonstrated to be under control** at larger scale or longer timelines: (1) the public, unauthenticated API Gateway endpoint has no request throttling, so cost (and the associated security exposure, per Step 47 F2/F3) is not actually bounded if traffic increased; (2) CloudWatch Lambda log retention is "Never Expire" and S3 has no version-lifecycle rule, so storage cost will grow monotonically over the project's lifetime with nothing currently limiting it — currently negligible in absolute terms (~5 MB logs, ~724 KB S3), but the *mechanism* for staying low-cost indefinitely is not yet in place, only the current low-usage level.
- Given real $0 billing evidence plus two identified-but-currently-negligible unbounded-growth mechanisms, this is assessed as **DEMONSTRATED for the project's life to date**, with an explicit note that continued low cost depends on either continued low usage or a future step adding the missing bounds (throttling, log/version retention policies) — not yet **PARTIALLY DEMONSTRATED**, since nothing found actually failed the constraint; but also not asserted as permanently guaranteed regardless of future usage patterns.

No exact monthly bill is invented beyond the $0.00 figure Cost Explorer itself returned.

---

## 19. Explicit limitations

- DynamoDB `ItemCount`/`TableSizeBytes` are AWS's own ~6-hour-stale estimates, not a live count — noted inline in the evidence JSON.
- Step Functions execution count (26) was retrieved via `list_executions` with `maxResults=1000`; since the true count is far below that cap, this is a complete count, not a sample — but the script itself does not assert completeness beyond what the cap allows.
- Cost Explorer's granularity here is service-level, monthly; no per-resource or per-day cost breakdown was requested (not needed given the $0.00 result, and avoids risking any billing-configuration mutation).
- CloudWatch custom metric *series* count (11) was retrieved via `list_metrics`, which itself does not report per-metric ingestion volume or cost directly — this audit infers proximity to the free-tier boundary (10 metrics) from the series count, not from a billed-metrics API.
- No attempt was made to determine whether `CloudLabUser` (the pre-existing, unrelated IAM user discovered in Step 48) has any cost implications — out of scope, not a SentinelGrid resource.
- This audit reflects a single point in time (2026-09-18); cost and resource state can change with any future step that deploys, tests, or exercises the system.

---

## 20. Recommended next engineering step

Two low-risk, narrowly-scoped follow-ups are suggested by this audit's findings (not performed here, per the read-only scope of this step):
1. Add explicit CloudWatch Logs retention (e.g., 30-90 days) to the 7 Lambda log groups — bounds future log storage growth with no risk to current functionality.
2. Add an S3 lifecycle rule to the deployment bucket to expire noncurrent object versions after a reasonable window (e.g., 30-90 days), retaining at least one or two recent noncurrent versions for rollback safety.

Both directly address the two (3)-classified "potentially significant over time" cost drivers identified in §15, without touching the live data plane, application behavior, or security posture beyond what Step 47 already recommended (API Gateway throttling, separately).
