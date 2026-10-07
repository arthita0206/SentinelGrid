# SentinelGrid — Step 48: Dedicated Least-Privilege Operator Identity

**Status: PARTIAL / BLOCKED at the authentication phase.** A dedicated least-privilege IAM role (`SentinelGridOperatorRole`) was designed, previewed via a CloudFormation change set, and successfully deployed. However, establishing a working, non-root **authentication path** into that role could not be completed automatically — AWS unconditionally disallows the account root user from calling `sts:AssumeRole`, and this account has no IAM Identity Center / SSO instance and is not an AWS Organizations member, so there is currently no non-root identity available to assume the role. The remaining automatable option (a minimal IAM user with one scoped permission) requires creating a long-lived access key, which was explicitly deferred to the account owner's decision rather than created automatically. **Root has not been retired.** Root usage for this project continues until a human decides how to complete Phase 6.

---

## 1. What exists now

| Resource | Status |
|---|---|
| `SentinelGridOperatorRole` (IAM role) | **Deployed** — CloudFormation stack `sentinelgrid-operator-identity`, `CREATE_COMPLETE` |
| Trust policy | Allows `arn:aws:iam::401429382000:root` (the account) to `sts:AssumeRole` — the standard AWS pattern; in practice unusable by root itself (see §3) |
| Permissions | 9 named inline policies, fully scoped (see §5) |
| `sentinelgrid-operator` CLI profile (`~/.aws/config`) | Configured (`role_arn` + `source_profile=sentinelgrid`) but **not yet functional** — nothing can currently assume the role |
| IAM user for the operator | **Not created** — deferred, see §4 |
| Access key | **Not created** |

The role itself poses no security exposure sitting idle: nothing in this account can currently assume it (root is blocked by AWS itself; no other identity exists), so it is inert until Phase 6 is completed by a human decision.

---

## 2. Why a role (not IAM Identity Center, not a plain IAM user) was the initial design

Phase 1 read-only precheck found:

```
aws iam list-account-aliases        -> []  (no alias set)
aws sso-admin list-instances        -> []  (no IAM Identity Center instance)
aws organizations describe-organization -> AWSOrganizationsNotInUseException
aws iam list-users                  -> one pre-existing user: CloudLabUser
                                        (an unrelated lab-platform artifact,
                                        not a SentinelGrid identity; not
                                        touched by this step)
```

**IAM Identity Center (Option 1, most preferred)** requires either an existing instance or bootstrapping one, which in a standalone (non-Organizations) account requires a Console-driven setup step AWS does not expose via a create-instance API. This is exactly the kind of "manual Console action involving account-level identity management" the task's Critical Safety Rule says to stop before and report — see §7 for the exact manual steps if the account owner wants to pursue this later.

**A dedicated IAM role (Option 2)** was therefore selected and built: `SentinelGridOperatorRole`, trusting the account (root) as principal, carrying only the permissions in §5. This is the safer, more auditable choice over a plain IAM user because in this design the role — not any human credential — is the actual boundary of what the operator identity can do; whatever eventually authenticates into it (see §4) inherits exactly this policy set and nothing more.

**A plain IAM user with directly-attached permissions (Option 3)** was explicitly deprioritized per the task's own instructions ("Do NOT blindly choose IAM user just because it is simple").

---

## 3. The blocker: root cannot assume any role

After deploying the role, `aws sts get-caller-identity --profile sentinelgrid-operator` was tested (a chained profile: `role_arn=arn:aws:iam::401429382000:role/SentinelGridOperatorRole`, `source_profile=sentinelgrid`). It failed:

```
An error occurred (AccessDenied) when calling the AssumeRole operation:
Roles may not be assumed by root accounts.
```

This is **not** a trust-policy misconfiguration — it is an unconditional restriction AWS STS enforces for the account root user specifically, regardless of the role's `Principal`/`Condition`. No trust-policy change can work around it. Since the current identity operating this entire account is root (confirmed in Step 47 and reconfirmed at the start of Step 48), there is, today, no principal capable of assuming `SentinelGridOperatorRole` at all.

---

## 4. Remaining path, and why it was not taken automatically

The only remaining automatable authentication mechanism, given no SSO and root's inability to assume roles, is: create a minimal IAM user (e.g. `SentinelGridOperator`) whose **only** permission is `sts:AssumeRole` on `SentinelGridOperatorRole`'s exact ARN — carrying no direct service permissions itself — and issue it one access key so a human can authenticate as that user, then immediately assume the role for actual work. This keeps the blast radius of the long-lived credential to "can assume one specific limited role," while the real least-privilege permission set stays entirely on the role already built and verified in §5.

This still requires creating a long-lived access key. The task's standing instruction is explicit: **"Do not create long-lived access keys unless absolutely required by the available authentication mechanism and explicitly justified."** That condition is now genuinely met (empirically, not assumed) — but because credential issuance is a meaningfully different and more sensitive action than deploying a scoped IAM role, this was surfaced to the project owner as an explicit decision point rather than performed automatically. **The owner chose to stop here** rather than proceed with user/key creation in this session. See §9 for the recommended next step.

---

## 5. Operator role permissions (Phase 10 table)

All 9 inline policies on `SentinelGridOperatorRole`, categorized Deployment (D) / Validation (V) / Read-only (R):

| Policy | Action(s) | Resource | Condition | Category | Reason | Risk if broadened |
|---|---|---|---|---|---|---|
| CloudFormationDeploy | Stack lifecycle (Create/Update/Delete/ChangeSet/Describe/GetTemplate/Tag) | `stack/sentinelgrid-*/*` | — | D | Deploy/update/inspect only SentinelGrid's own stacks | Broadened to `*`, could manage any stack in the account |
| CloudFormationDeploy | `ValidateTemplate`, `GetTemplateSummary`, `ListStacks` | `*` | — | R | No resource-level ARN support in this API | Low — these are inherently account-wide read actions |
| IAMForDeployment | Role CRUD (`CreateRole`/`PutRolePolicy`/etc.) | `role/SentinelGrid*` | — | D | CFN `CAPABILITY_NAMED_IAM` stacks manage these 10 roles' lifecycle | Broadened to `*`, could create/alter ANY IAM role — this is the single most dangerous permission category in the whole policy set |
| IAMForDeployment | `iam:PassRole` (Lambda roles) | 7 exact Lambda-role ARNs | `iam:PassedToService=lambda.amazonaws.com` | D | CFN must pass each Lambda's execution role when creating/updating the function | Broadened to `*`, is a privilege-escalation vector (could pass any role to any Lambda) |
| IAMForDeployment | `iam:PassRole` (Step Functions) | 1 exact ARN (`RemediationWorkflowRole`) | `iam:PassedToService=states.amazonaws.com` | D | Same, for the state machine | Same |
| IAMForDeployment | `iam:PassRole` (IoT) | 2 exact ARNs | `iam:PassedToService=iot.amazonaws.com` | D | Same, for the 2 IoT topic rules | Same |
| LambdaDeployAndInvoke | Function CRUD, `AddPermission`/`RemovePermission`, `GetPolicy` | `function:SentinelGrid*` | — | D | Deploy/update the 7 SentinelGrid Lambdas | Broadened, could manage any Lambda in the account |
| LambdaDeployAndInvoke | `InvokeFunction` | `function:SentinelGrid*` | — | V | Controlled validation invokes (same pattern as Step 46 Test Case B) | Could invoke arbitrary functions if broadened |
| LambdaDeployAndInvoke | `ListFunctions` | `*` | — | R | No resource-level ARN support | Low |
| DynamoDB | Table CRUD, PITR/TTL describe | `table/SentinelGrid*` | — | D | Manage the 4 SentinelGrid tables' schema/lifecycle | Broadened, could manage any table |
| DynamoDB | `Scan`/`Query`/`GetItem` (read only — no Put/Update/Delete) | `table/SentinelGrid*` | — | V/R | Inspect deployed data for validation, never mutate it | Data-mutation risk if Put/Update/Delete were ever added |
| DynamoDB | `ListTables` | `*` | — | R | No resource-level ARN support | Low |
| SQS | Queue CRUD/attributes/tags | `SentinelGrid*` | — | D | Manage the 2 SentinelGrid queues | Broadened, could manage any queue |
| SQS | `ListQueues` | `*` | — | R | No resource-level ARN support | Low |
| IoT | Topic-rule CRUD | `rule/sentinelgrid_*` | — | D | Manage the 3 SentinelGrid IoT rules | Broadened, could manage any IoT rule |
| IoT | `ListTopicRules` | `*` | — | R | No resource-level ARN support | Low |
| IoT | `iot-data:Publish` | `topic/sentinelgrid/*` | — | V | Controlled test telemetry publishing (same topic namespace the simulator already uses) | Broadened, could publish to any IoT topic |
| StepFunctions | State machine CRUD, `StartExecution` | `stateMachine:SentinelGrid*` | — | D/V | Deploy + controlled-test execution starts | Broadened, could start executions on any state machine |
| StepFunctions | `DescribeExecution`/`GetExecutionHistory` | `execution:SentinelGrid*:*` | — | V | Inspect controlled-test execution results | Low if broadened — read only |
| StepFunctions | `ListStateMachines` | `*` | — | R | No resource-level ARN support | Low |
| ApiGateway | `GET`/`POST`/`PUT`/`PATCH`/`DELETE` | `/apis/m1uqr1hsq3` + `/apis/m1uqr1hsq3/*` | — | D | Manage the one existing SentinelGrid HTTP API | Broadened, could manage any API in the account |
| ApiGateway | `GET` | `/apis` (collection) | — | R | AWS's own collection-level ARN pattern for listing | Low |
| CloudWatchAndSNS | `PutMetricAlarm`/`DeleteAlarms`/`PutDashboard`/`DeleteDashboards`/`GetDashboard` | `alarm:SentinelGrid-*`, `dashboard/SentinelGrid-*` | — | D | Manage the 1 alarm + 1 dashboard | Broadened, could manage any alarm/dashboard |
| CloudWatchAndSNS | `GetMetricData`/`ListMetrics`/`DescribeAlarms`/`ListDashboards` | `*` | — | R | No resource-level ARN support (same documented constraint as the service roles, Step 43/47) | Low — read only |
| CloudWatchAndSNS | Topic CRUD/attributes (no Publish/Subscribe) | `SentinelGrid-*` | — | D | Manage the 1 SentinelGrid SNS topic | Broadened, could manage any topic; Publish/Subscribe deliberately excluded entirely |
| CloudWatchAndSNS | `ListTopics` | `*` | — | R | No resource-level ARN support | Low |
| S3DeployBucketAndLogs | `GetBucketVersioning`/`GetObject`/`PutObject` | `sentinelgrid-lambda-deploy-401429382000` (+`/*`) | — | D | Upload/verify Lambda deployment packages | Broadened, could read/write any bucket |
| S3DeployBucketAndLogs | `DescribeLogGroups`/`DescribeLogStreams`/`FilterLogEvents`/`GetLogEvents` | `log-group:/aws/lambda/SentinelGrid*` | — | V/R | Latency/validation analysis (same pattern as `scripts/step36_analyze_results.py`) | Broadened, could read any Lambda's logs |

**Explicitly NOT granted anywhere:** `iam:*`, `organizations:*`, `account:*`, `billing:*`, `AdministratorAccess`, any `Action:"*"` statement, any `iam:PassRole` on `Resource:"*"`, `dynamodb:PutItem`/`UpdateItem`/`DeleteItem`, `sns:Publish`, `sns:Subscribe`, anything touching the CloudFront/S3 frontend-hosting stack (Step 45 remains explicitly out of scope), anything touching Lambda concurrency settings, anything touching the Step Functions ASL definition content.

Every `Resource:"*"` statement above is one of 13 specific read-only actions with no AWS resource-level ARN support at all — the exact same documented, unavoidable-constraint pattern already established for the service execution roles in Steps 43/47 (`cloudwatch:PutMetricData`/`GetMetricData`). None is a broadened write/mutate permission.

No numerical security score is assigned to this design, per the task's standing instruction.

---

## 6. Authentication / profile configuration

`~/.aws/config` now contains:

```
[profile sentinelgrid-operator]
role_arn = arn:aws:iam::401429382000:role/SentinelGridOperatorRole
source_profile = sentinelgrid
region = ap-south-1
```

This profile is standard AWS CLI role-chaining configuration — no credentials of any kind are stored in it, in this file, in source code, `.env` files, config YAML, Git, shell scripts, or Lambda environment variables. It currently **cannot succeed** (`source_profile=sentinelgrid` resolves to root, which cannot assume roles). It is left in place as ready-to-use scaffolding: once a non-root identity exists (either a `SentinelGridOperator` IAM user per §4, or an IAM Identity Center permission set per §7), `source_profile` (or the equivalent SSO configuration) is the only thing that needs to change for `aws sts get-caller-identity --profile sentinelgrid-operator` to resolve to a non-root ARN.

---

## 7. Manual AWS Console actions available to complete this transition

Two independent paths exist to finish Phase 6; neither was performed automatically.

**Path A — IAM Identity Center (AWS's own most-preferred mechanism, but requires Console/Organizations setup this account doesn't have):**
1. AWS Console → IAM Identity Center → "Enable" (this will offer to create an AWS Organization around this account, since Identity Center requires one).
2. Create a permission set named e.g. `SentinelGridOperatorAccess` that simply trusts/maps to the existing `SentinelGridOperatorRole` permissions (or recreate the same 9 policies as a permission set).
3. Assign a user (created in Identity Center's own directory, or federated) to that permission set for this account.
4. Run `aws configure sso` to set up a `sentinelgrid-operator` profile using SSO, replacing the current role-chained config.

**Path B — Minimal IAM user + access key (the path this step stopped short of, per §4):**
1. `aws iam create-user --user-name SentinelGridOperator`
2. Attach an inline policy to that user granting **only** `sts:AssumeRole` on `arn:aws:iam::401429382000:role/SentinelGridOperatorRole`.
3. `aws iam create-access-key --user-name SentinelGridOperator` (the only long-lived credential this design would ever create).
4. Store the resulting key in a new `[sentinelgrid-operator-user]` profile in `~/.aws/credentials` (never in this repository), then update the `sentinelgrid-operator` profile in `~/.aws/config` to use `source_profile = sentinelgrid-operator-user` instead of `sentinelgrid`.
5. Verify: `aws sts get-caller-identity --profile sentinelgrid-operator` — must return `arn:aws:sts::401429382000:assumed-role/SentinelGridOperatorRole/...`, never `:root`.

This document intentionally does not perform either path — it is a decision for the account owner (see §9).

---

## 8. What was verified this step

- `SentinelGridOperatorRole` exists, `CREATE_COMPLETE`, with the exact 9 inline policies designed (§5).
- The CloudFormation change set previewed before execution showed exactly one resource added (`SentinelGridOperatorRole`, `AWS::IAM::Role`) — no surprise resources.
- `aws sts assume-role` against the new role from the root identity fails with `AccessDenied: Roles may not be assumed by root accounts` — confirmed live, not assumed from documentation alone.
- None of the 10 existing SentinelGrid service-execution roles were modified (verified where AWS session access allowed — see the Step 48 final report for the exact scope of what was re-confirmed live vs. designed-and-not-yet-re-verified due to a mid-step AWS session expiry).
- The local Step Functions ASL template hash is unchanged: `0135027f4b0c98a72964c63494097e26e37eafe0c912a5f93355f87f855d1d3e`.
- Full `pytest` suite plus the new `tests/test_operator_identity.py` (18 tests, all static/template-based, no AWS credentials required) pass.

---

## 9. Recommended next step

This is a decision for the account owner, not something to default into:

- **If this AWS account is expected to be long-lived / used beyond this academic project**: pursue Path A (IAM Identity Center) — it is the only option here that avoids a long-lived credential entirely, and is AWS's own recommended mechanism.
- **If a quick, scriptable path is preferred and the account owner accepts one narrowly-scoped long-lived access key**: pursue Path B, exactly as designed in §4/§7 — the key's blast radius is limited to "assume one specific least-privilege role," which is a materially smaller risk than the current status quo of operating entirely as root.
- **Either way**: once Phase 6 succeeds, update this document and re-run `scripts/step48_operator_identity_audit.py` to capture the working, non-root identity as evidence, and revisit `docs/security-audit.md` §15/§16 (F1) to reflect the new operating identity.

Root itself was not touched, locked, or modified in any way during this step, and remains fully available for account-level tasks (billing, support, MFA management) as it always was.
