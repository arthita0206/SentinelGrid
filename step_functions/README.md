# Step Functions (local design only — not deployed)

## Purpose

This directory holds the future AWS Step Functions orchestration for
SentinelGrid's remediation workflow. As of Step 14, it contains **only a
local Amazon States Language (ASL) definition and its structural
validation tests** — no Step Functions state machine, IAM role, or
Lambda function backing it has been created or deployed to AWS. This
mirrors how the anomaly detector and remediation engine were each built
and tested locally before any cloud integration was attempted (see
`lambda_functions/anomaly_detector/README.md` and
`remediation/README.md`).

The workflow orchestrates the existing local pipeline —

```
detect_telemetry() -> RemediationEngine.plan() -> RemediationExecutor.execute()
```

— once each stage is eventually wrapped in its own Lambda function.
**None of those Lambdas exist yet.** This step only proves the shape of
the orchestration.

## File

- **`remediation_workflow.json`** — the ASL state-machine definition.

## Architectural principle: orchestrate, don't duplicate

Step Functions decides *which state runs next*; it never decides *what
counts as an anomaly* or *which remediation action applies*. Every
`Choice` state in `remediation_workflow.json` branches on exactly one
pre-computed boolean field — `detected`, `remediation_plan.automatic`, or
`verification.verified` — and nothing in the definition contains a
`StringEquals`-style rule that would mean fault types or action names had
been reimplemented in ASL. The actual fault-to-action mapping stays
entirely inside `remediation/engine.py`; this file only calls out to it
(via a placeholder Lambda) and reacts to what it decided.

## State-machine flow

```
                              CheckAnomaly
                            (detected == true?)
                          /                    \
                    false                        true
                        |                          |
                    NoAnomaly               PrepareRemediationPlanning
                    (Succeed)                        |
                                               PlanRemediation
                                          (placeholder: RemediationPlanner)
                                                      |
                                          CheckAutomaticRemediation
                                            (automatic == true?)
                                          /                      \
                                    false                          true
                                        |                            |
                                    Escalate                ExecuteRemediation
                                                      (placeholder: RemediationExecutor)
                                                    /                        \
                                              (Catch: failure)          (success)
                                                    |                        |
                                                Escalate            VerifyRemediation
                                                              (placeholder: VerifyRemediation)
                                                            /                        \
                                                      (Catch: failure)           (success)
                                                            |                        |
                                                        Escalate            CheckVerification
                                                                              (verified == true?)
                                                                            /                  \
                                                                      false                     true
                                                                        |                         |
                                                                    Escalate                  Success
```

`PrepareRemediationPlanning` and `CheckVerification` are two small
supporting states beyond the eight named in this project's design brief
(`CheckAnomaly`, `PlanRemediation`, `CheckAutomaticRemediation`,
`ExecuteRemediation`, `VerifyRemediation`, `Escalate`, `Success`,
`NoAnomaly`). Each exists for a structural reason, not a business one:

- **`PrepareRemediationPlanning`** (`Pass`) wraps the raw `DetectionResult`
  input under a `detection_result` key. This is needed because a `Task`
  state's result merges back into the state's *own raw input* via
  `ResultPath` — without this wrapping step, `PlanRemediation`'s output
  would sit alongside the detection's fields at the top level instead of
  producing the documented `{detection_result, remediation_plan}` shape.
- **`CheckVerification`** (`Choice`) exists because a `Task` state cannot
  branch on its own result in ASL — something has to inspect
  `verification.verified` afterward, and that must be a `Choice` state.

## Input/output contract

| Stage | Input | Output |
|---|---|---|
| `CheckAnomaly` | `DetectionResult` | unchanged `DetectionResult` |
| `PlanRemediation` | `DetectionResult` | `RemediationPlan` |
| `ExecuteRemediation` | `RemediationPlan` | `RemediationResult` |
| `VerifyRemediation` | everything accumulated so far | `{"verified": true \| false}` |

Data accumulates rather than being replaced at each step, so nothing is
ever discarded:

```
DetectionResult
  -> {detection_result}
  -> {detection_result, remediation_plan}
  -> {detection_result, remediation_plan, remediation_result}
  -> {detection_result, remediation_plan, remediation_result, verification}
```

`node_id`, `subsystem`, `fault_type`, `severity`, and `timestamp` are
never dropped — they remain reachable under `detection_result` (and are
also copied into `remediation_plan`/`remediation_result` by the
Lambdas that will eventually populate those fields, matching
`remediation/models.py`'s existing field shapes).

## Automatic vs. escalation paths

`CheckAutomaticRemediation` branches purely on `remediation_plan.automatic`
— a field `RemediationEngine.plan()` already computes (see
`remediation/engine.py`). This one boolean already covers every case this
project's remediation policy defines as non-automatic:

- `HIGH`/`CRITICAL` severity `NODE_OVERLOAD`
- `INVALID_INPUT`
- an unknown/unsupported fault type
- a `DetectionResult` missing required fields

None of that policy is re-encoded here — the state machine simply trusts
the flag it's given.

## Error handling

- **`ExecuteRemediation` failure** (the placeholder Lambda raising/erroring)
  is caught (`Catch: States.ALL`) and routed to `Escalate` — a remediation
  that failed to execute is never treated as successful.
- **`VerifyRemediation` failure** is handled two ways, both routing to
  `Escalate`: a *technical* failure of the verification task itself
  (caught the same way as above), and a *structural* `{"verified": false}`
  result (routed by `CheckVerification`'s `Default`). Neither is ever
  reported as `Success`.
- No retries or backoff policies are defined yet — this step proves the
  basic orchestration shape only; operational retry behavior is an
  explicitly separate, later design decision.

## Current local-only status

- No Step Functions state machine has been created or deployed.
- No IAM role for Step Functions exists.
- No `RemediationPlanner`, `RemediationExecutor`, or `VerifyRemediation`
  Lambda exists — the `Resource` fields in `remediation_workflow.json`
  are CloudFormation `Fn::Sub`-style placeholders (`${RemediationPlannerFunctionArn}`,
  etc.), not real ARNs, and this file cannot be submitted to AWS as-is.
  Account-specific ARNs (e.g. containing this project's AWS account id)
  are deliberately never hardcoded here.
- The existing local `RemediationExecutor` (`remediation/engine.py`)
  remains a safe, AWS-independent simulation and was not modified by this
  step.

## ASL validation approach and its limits

`tests/test_step_functions_workflow.py` validates this definition in two
ways:

1. **Static structural checks** — the JSON parses, `StartAt`/`States`
   exist, every state referenced by a `Next`/`Default`/`Catch` actually
   exists, every state is reachable from `StartAt`, the eight required
   conceptual states are present, every `Choice` has a `Default`, every
   `Task` has a `Resource`, no account-specific Lambda ARN appears
   anywhere, and no `Choice` branches on anything other than the three
   orchestration-owned booleans.
2. **A small, purpose-built local trace interpreter** that supports only
   the exact state features this one workflow uses (`Choice` with
   `BooleanEquals`/`Default`, `Pass`, `Task` with a simulated
   output/failure, `Succeed`). Given representative sample inputs (normal
   telemetry, automatic remediation, HIGH/CRITICAL overload,
   `INVALID_INPUT`, an execution failure, a verification failure, a
   verification-task failure), it walks the graph and confirms each one
   reaches the expected terminal state.

**This does not prove AWS-side validity or deployability.** It is not a
general ASL interpreter, and passing these tests is not equivalent to
AWS Step Functions' own `validate-state-machine-definition` check or an
actual deployment. That AWS-side validation is explicitly deferred to a
later step, after this design is reviewed and approved.

## Future AWS integration point

A later step will: create `RemediationPlanner`, `RemediationExecutor`,
and `VerifyRemediation` Lambda functions (thin adapters, mirroring
`lambda_functions/anomaly_detector/handler.py`, over the existing
`remediation/engine.py`); create a CloudFormation template that deploys
the Step Functions state machine, substituting the real Lambda ARNs into
this ASL definition via `Fn::Sub`; create the least-privilege IAM role
Step Functions needs to invoke those three Lambdas; and wire the
anomaly-detection Lambda to start an execution of this state machine.
None of that exists yet, and none of it is created by this step.
