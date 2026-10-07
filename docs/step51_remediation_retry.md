# SentinelGrid — Step 51: Configurable Remediation Retry & Escalation (FR-5.4)

This step addresses the FR-5.4 gap identified in Step 50's SRS traceability pass: the deployed Step Functions remediation workflow previously escalated immediately on any single recovery failure, with no retry at all. This document records the design, the exact counter semantics, local and live validation evidence, and honest limitations.

Evidence: [`docs/step51_retry_validation_results.json`](step51_retry_validation_results.json) (live evidence), 46 tests in [`tests/test_step_functions_workflow.py`](../tests/test_step_functions_workflow.py) (local evidence), the deployed [`step_functions/remediation_workflow.json`](../step_functions/remediation_workflow.json).

---

## 1. FR-5.4 implementation summary

**Requirement (SRS FR-5.4):** "Retry failed recovery up to configurable maximum and escalate."

**Before Step 51:** `ExecuteRemediation`/`VerifyRemediation` each had a `Catch` (`States.ALL`) routing straight to `Escalate` on any Lambda-level failure. There was no `Retry` block, and — a separate finding discovered during this step's own inspection — the ASL never even checked `RemediationResult.success`; it proceeded from `ExecuteRemediation` to `VerifyRemediation` unconditionally regardless of whether the executor reported success or failure.

**After Step 51:** A bounded, configurable retry loop wraps the Execute→Verify cycle. Any of four failure conditions (Execute throws, Execute returns `success != true`, Verify throws, Verify returns `verified != true`) routes to a shared `CheckRetryLimit` decision point; if the retry budget is not exhausted, the workflow increments a counter and re-attempts `ExecuteRemediation`; once exhausted, it escalates exactly as before.

## 2. Retry semantics

**Interpretation of "maximum":** `max_retries` counts retries **after** the initial attempt, exactly as specified in the task. `max_retries = 2` means: Attempt 1 → fail → Retry 1 → fail → Retry 2 → fail → Escalate (3 total attempts). Verified exactly via `test_max_retries_two_allows_exactly_two_retries_then_escalates` (asserts `visited.count("ExecuteRemediation") == 3`).

**Counter semantics:**
- `retry_count` initializes to `0` in `PrepareRemediationPlanning`, once, at the start of the accumulating envelope.
- On any failure, `CheckRetryLimit` compares `retry_count < max_retries` (via ASL's native `NumericLessThanPath` — comparing two runtime JSONPath values, not a literal).
- If true: `IncrementRetryCount` sets `retry_count = retry_count + 1` (via the `States.MathAdd` intrinsic function) and loops back to `ExecuteRemediation`.
- If false (`retry_count >= max_retries`): routes to `Escalate`.
- No off-by-one error: for `max_retries = N`, exactly `N` retries occur (N+1 total attempts) before escalation — verified for N = 0 through 5 in `test_valid_max_retries_in_bounds_is_honored_exactly`.

## 3. Default / min / max retry configuration

| | Value |
|---|---|
| Default (`max_retries` absent or invalid) | **2** (matches the task's "preferably 2" guidance, and this project's own precedent of 2 being a recurring "small controlled sample" size in Steps 36/46) |
| Minimum | **0** (zero retries — a single attempt, immediate escalation on failure — explicitly allowed, not rejected) |
| Maximum | **5** (bounds the worst-case total attempts to 6, preventing an unbounded/runaway retry loop) |

**Why 2 as the default, not another value:** it directly matches the task's own explicit preference ("preferably 2"), keeps the default behavior modest (3 total attempts) appropriate for a low-cost student-account project (Step 49's NFR-9 cost posture), and avoids silently multiplying Lambda invocation/DynamoDB-write volume for callers that never opted into retry behavior.

**Bounds-validation design:** any value that is absent, non-numeric, negative, or greater than 5 uniformly falls back to the default (2) — **not** clamped to the nearest boundary. This was a deliberate choice: clamping (e.g., treating `1000` as `5`) would require guessing what the caller "meant," while falling back to a single, well-documented default treats every invalid input uniformly and predictably. Verified for 7 distinct invalid cases (`None`, `"not-a-number"`, `-1`, `-100`, `6`, `1000`, `[1, 2]`) in `test_invalid_or_missing_max_retries_falls_back_to_documented_default`.

## 4. Input contract (backward compatibility)

**Determined the actual current contract before designing anything** (Phase 1): `lambda_functions/remediation_trigger/handler.py`'s `lambda_handler` calls `stepfunctions:StartExecution` with `input=json.dumps(decision.detection_result)` — the exact, **unwrapped** `DetectionResult.to_dict()`, confirmed by direct code inspection. Every historical test (Steps 19, 23, 27, 32, 33, 34, 46) and the real deployed trigger both send this same flat shape, with `detected` at the top level (which `CheckAnomaly`'s very first Choice reads directly).

**Deliberate deviation from the task's illustrative example:** the task's own example showed `{"detection_result": {...}, "max_retries": 2}` — a *wrapped* shape. This was **not** adopted, because it would have broken `CheckAnomaly`'s `$.detected` read on every existing caller (a materially larger, riskier change than necessary) and contradicted the task's own higher-priority instruction: *"Do NOT blindly change the trigger payload format if it would break the existing pipeline."* Instead, `max_retries` is supported as an **optional, flat, sibling field** alongside the existing `DetectionResult` fields:

```json
{
  "detected": true,
  "fault_type": "NODE_OVERLOAD",
  "severity": "MEDIUM",
  "node_id": "power-007",
  "subsystem": "power",
  "timestamp": "2026-01-01T00:05:00+00:00",
  "reason": "...",
  "evidence": {...},
  "max_retries": 2
}
```

This is 100% backward compatible: every caller that omits `max_retries` (including the deployed `RemediationTrigger` Lambda, unmodified) gets the documented default (2) automatically — no application code change was needed anywhere. `lambda_functions/remediation_trigger/`, `remediation/engine.py`, and all three remediation Lambda adapters (planner.py/executor.py/verifier.py) are **unmodified**.

## 5. Failure conditions that trigger retry

Per Phase 3's explicit list, all four are implemented:
1. `ExecuteRemediation` Lambda invocation throws (`Catch` → `CheckRetryLimit`).
2. `ExecuteRemediation` returns `RemediationResult.success != true` (new `CheckExecutionSuccess` Choice → `CheckRetryLimit`; **this check did not exist at all before Step 51** — a second, independently-discovered gap).
3. `VerifyRemediation` Lambda invocation throws (`Catch` → `CheckRetryLimit`).
4. `VerifyRemediation` returns `verified != true` (`CheckVerification`'s `Default` → `CheckRetryLimit`, was `Escalate` before).

## 6. Conditions that bypass retry and escalate directly

- **Non-automatic remediation** (`CheckAutomaticRemediation`'s `Default`): HIGH/CRITICAL `NODE_OVERLOAD`, `INVALID_INPUT`, and any other escalation-required plan never reaches `ExecuteRemediation` at all — untouched by Step 51, verified in `test_non_automatic_plan_never_enters_retry_loop` (even with `max_retries=5` explicitly supplied, to prove the budget is irrelevant here).
- **`NoAnomaly`**: `detected == false` terminates immediately, as before.
- **Planner failure**: deliberately **out of scope** for this step. `PlanRemediation` has no `Catch` and is not part of the retry loop — an unexpected Planner failure remains an unhandled state-machine error, exactly as before Step 51. This is a genuine, pre-existing gap distinct from FR-5.4 (which is about retrying *recovery*, not *planning*); fixing it would be scope creep beyond "make FR-5.4 real," so it is documented here rather than silently addressed.

## 7. Step Functions ASL changes

6 new states added: `ValidateMaxRetries`, `UseProvidedMaxRetries`, `UseDefaultMaxRetries`, `CheckExecutionSuccess`, `CheckRetryLimit`, `IncrementRetryCount`. 3 existing states' failure-routing changed (`ExecuteRemediation`'s `Catch`, `VerifyRemediation`'s `Catch`, `CheckVerification`'s `Default` — all previously `Escalate`, now `CheckRetryLimit`). No existing `InputPath`/`ResultPath` on `PlanRemediation`/`ExecuteRemediation`/`VerifyRemediation` changed. No Lambda `Resource` ARN changed.

**Why native ASL `Retry` was not used:** AWS Step Functions' `Retry.MaxAttempts` accepts only a literal integer in the state machine definition — never a runtime JSONPath value like `$.max_retries` (confirmed both by the task's own explicit warning and independently reasoned through during design). Per-execution runtime configurability therefore required an explicit bounded counter implemented with `Choice` + `Pass`, using the native `NumericLessThanPath` comparison operator (comparing two runtime paths) and the `States.MathAdd` intrinsic function (ASL's native arithmetic helper — ASL has no bare `+` operator) for the increment. This is valid, deployable ASL — proven live in Phase 10 (Test A), not just asserted.

## 8. Before/after ASL SHA

| | Local template (`step_functions/remediation_workflow.json`) | Live deployed (`describe-state-machine`, normalized) |
|---|---|---|
| **Before Step 51** | `0135027f4b0c98a72964c63494097e26e37eafe0c912a5f93355f87f855d1d3e` | `b861896cbc7ed4a75de1ee19aabf515907dab8e1668a3e0bcb9fb4f6ea09dc7e` |
| **After Step 51** | `d1607fa3840011d4f84c8d4c009c8a270f5fe24f5030fa3e720150b113a5e516` | `0aa9657413b6d9b1dd743784f7559d7dddc93f47ce588682b008576fc4964a1b` |

As established in Step 47, the local-template and live-deployed hashes are never expected to be equal to each other even for identical content — the local file retains unresolved `${...}` CloudFormation placeholder tokens that `DefinitionSubstitutions` resolves to real Lambda ARNs only at deploy time. Both pairs changed together consistently, confirming the deployed content matches the local source exactly (modulo that expected, designed substitution).

## 9. Infrastructure changes

**None.** `infrastructure/remediation-workflow.yaml` required no changes: it references the ASL file only via `DefinitionS3Location` + `DefinitionSubstitutions` (the same 3 imported Lambda ARNs, unchanged), and grants the same `lambda:InvokeFunction` permission on the same 3 ARNs — no new Lambda, no new permission, no IAM change was needed since the retry mechanism only adds `Choice`/`Pass` states (no new AWS API calls) around the existing 3 Task states.

## 10. Local validation results

**46/46 tests pass** in `tests/test_step_functions_workflow.py` (23 pre-existing structural/fixture tests, updated where the new states legitimately changed their expected `visited` sequences, plus 24 new Step-51-specific tests covering:
- `max_retries` = 0, 1, 2, 3, 4, 5 (exact honoring, no off-by-one)
- Successful first attempt (retry states never touched)
- Success after one retry (verification fails then succeeds)
- Success after two retries (execution fails twice then succeeds)
- Failure after all retries exhausted (default budget, evidence preserved)
- 7 distinct malformed/missing/negative/excessive `max_retries` cases (all fall back to the default)
- Non-automatic plan never enters the retry loop (even with a generous budget)
- No `StartExecution` reference anywhere in the ASL (single-execution guarantee)
- No `fault_type`/`action_type`/`severity` referenced by any retry state (mapping logic not duplicated)

The local interpreter (`_trace_workflow`) was extended to support `IsPresent`/`IsNumeric`/`NumericGreaterThanEquals`/`NumericLessThanEquals`/`NumericLessThanPath`/`And`-composite Choice conditions and the `States.MathAdd` intrinsic function — documented in the test file's own module docstring as exactly what it does and does not prove (a local structural/logic check, not a claim of AWS-side ASL validity).

## 11. Live validation results

**Test A (success on first attempt): PERFORMED, PASSED.** A single Step Functions execution was started directly (the same "control-plane" pattern established in Step 32 / Step 46 Test Case B) with a synthetic `NODE_OVERLOAD`/`MEDIUM` `DetectionResult` plus `max_retries: 2`. Result: `SUCCEEDED`, visited states show `UseProvidedMaxRetries` (confirming the explicit `max_retries` was honored, not defaulted), zero retry-loop states touched, `retry_count: 0` in the final output, exactly one `NodeState` record (`RECOVERED`) and one `RecoveryEvents` record (`SUCCESS`) — no duplicates. This is real, live proof that the new ASL (including every new comparison operator and the `States.MathAdd` intrinsic) is valid, AWS-accepted Step Functions syntax, not just locally-interpreter-valid. Full evidence in `docs/step51_retry_validation_results.json`.

**Test B (retry behavior): NOT DEMONSTRATED LIVE — by design, not oversight.** Direct inspection of `remediation/engine.py`'s `RemediationExecutor.execute()` confirms it is a deterministic pure function that **always** returns `success=True, status=SUCCESS` for any plan with `remediation_required=True, automatic=True, escalation_required=False` (the only kind of plan that ever reaches `ExecuteRemediation`) — there is no code path, no input, no natural condition under which a genuine automatic plan's execution "fails" at the `RemediationResult` level. Likewise, `lambda_functions/remediation/verifier.py`'s `_verify()` only returns `verified: false` when `remediation_plan` and `remediation_result` fields fail to correspond — impossible for a real plan/result pair, since both are deterministically derived from the same underlying fault by the real deployed Lambdas. Per the task's own explicit instruction — *"If there is no safe way to force a live failure without changing production behavior: do NOT invent one... document live retry execution as NOT DEMONSTRATED"* — no Lambda code was modified and no artificial "always fail" hook was introduced. Retry behavior is instead validated exhaustively via the 24 local tests in §10.

**Test C (escalation after retries): NOT DEMONSTRATED LIVE.** Same reasoning as Test B — Test C's precondition (a real, live, repeatable recovery failure) was not available without violating the "do not manufacture a failure" constraint.

## 12. Recovery-attempt metric behavior

Inspected `lambda_functions/remediation/executor.py`'s `lambda_handler` (unmodified):
- **`RecoveryAttemptCount`**: incremented once per `ExecuteRemediation` invocation where `is_attempt` is true — correctly increments once per retry attempt too, since each retry re-invokes the same Lambda. This is the semantically **correct** behavior per this step's own framing ("a retry should be an additional recovery attempt, not an additional fault detection") — no metric code change needed.
- **`RecoverySuccessCount`**: incremented whenever `result.status == SUCCESS`, which — given `execute()`'s deterministic nature (§11) — is **every** attempt for an automatic plan, including retries triggered by a *verification* failure (where Execute itself keeps succeeding). This means `RecoverySuccessCount` can be incremented more than once for what is ultimately a single final recovery outcome, if a retry was triggered by verification rather than execution. **Documented as a known metric-semantics limitation, not fixed** — `shared/metrics.py`/`executor.py` were deliberately not modified, per this step's narrow ASL-focused scope.
- **`RecoveryLatency`**: computed from the original detection timestamp to the *current* Executor invocation's completion — on a late successful retry, this correctly reflects the *total* end-to-end latency including retry overhead, which is the semantically correct measurement. No change needed.
- **`UnresolvedFaultCount`**: only incremented on the final `Escalate` path (via the Trigger/Planner's own existing logic, unaffected by Step 51 — retries that ultimately succeed never reach `Escalate`, so no double-counting here).

## 13. Recovery-history behavior

Analyzed `lambda_functions/remediation/recovery_event_store.py`'s idempotency model against the retry scenario (Phase 12). The table's primary key is `(node_id, timestamp)` where `timestamp` is the **detection** timestamp — unchanged across every retry within one execution (all retries operate on the same original `DetectionResult`). A duplicate-key write is resolved by `_resolve_duplicate()`, which compares `status`/`success`/`automatic`/`action`/`fault_type`/`severity` between the existing and new item; a mismatch raises `RecoveryEventConflictError` (refusing to silently overwrite history).

**Determined this is safe, not corrupting, given the current deterministic executor:** since `RemediationExecutor.execute()` always returns the identical outcome for the identical plan (§11), every retry's `RecoveryEvents` write for the same key is **identical** to the first — the existing idempotency model correctly resolves it as `DUPLICATE_ALREADY_EXISTS` (an idempotent no-op), never a genuine conflict. A conflict would only become possible if a future, non-deterministic executor produced *different* outcomes across retries for the same key — a real, but currently unreachable, limitation of the schema. **The schema was not redesigned** (per explicit instruction); this reasoning is the documented answer to "does the existing idempotency model represent multiple attempts correctly" — yes, for the current deterministic executor, by collapsing them into one record; no code change was made or needed.

## 14. Idempotency behavior

The retry loop occurs **entirely inside the single Step Functions execution** — confirmed structurally (`test_retry_never_starts_a_new_step_functions_execution`: no state's `Resource` references `StartExecution`) and confirmed live (Test A's execution history shows a single `executionArn` throughout). `lambda_functions/remediation_trigger/`'s deterministic execution-name hashing and duplicate-`StartExecution` handling are completely unmodified and untouched by this step — retries never call `StartExecution` at all, so that idempotency layer is not even exercised by a retry.

## 15. Existing workflow regression

The full pre-existing successful path (`CheckAnomaly` → ... → `Success`) and the non-automatic escalation path (`CheckAutomaticRemediation` → `Escalate`) are both structurally unchanged and re-verified (13 pre-existing fixture tests, all passing with updated `visited` sequences reflecting only the added prefix states, never a changed decision). Live Test A additionally confirms this on the real deployed infrastructure, not just locally.

## 16. Files created/modified

**Modified:**
- `step_functions/remediation_workflow.json` — the retry/escalation implementation itself.
- `tests/test_step_functions_workflow.py` — interpreter extended (new Choice operators, `States.MathAdd`, list-based successive Task outcomes); 3 pre-existing tests updated (hardcoded `visited` sequence, `max_retries=0` made explicit, `max_steps` default raised 20→50); 24 new tests added.
- `tests/test_metrics.py` — one stale ASL SHA assertion updated to the new post-Step-51 value, with an explanatory comment (the Step-28 guard's original intent is preserved; only the expected hash changed).
- `tests/test_remediation_workflow_infrastructure.py` — one stale exact-state-set assertion updated to include the 6 new retry states.

**Created:**
- `scripts/step51_live_retry_validation.py`
- `docs/step51_remediation_retry.md` (this file)
- `docs/step51_retry_validation_results.json`

**Not modified** (confirmed unnecessary): `infrastructure/remediation-workflow.yaml`, `infrastructure/deploy-remediation-workflow.sh`, `lambda_functions/remediation/{planner,executor,verifier}.py`, `remediation/engine.py`, `remediation/models.py`, `lambda_functions/remediation_trigger/*`, any IAM policy, SQS/DynamoDB/API Gateway/frontend code.

## 17. AWS resources changed

**One resource updated in place**, confirmed via a previewed change set before execution (`Action: Modify`, `Replacement: False`): the `SentinelGridRemediationWorkflow` state machine's definition, via the `sentinelgrid-remediation-workflow` CloudFormation stack (`UPDATE_COMPLETE`). No new resource created, no resource deleted, no IAM role/policy changed (execution role ARN unchanged, confirmed: `arn:aws:iam::401429382000:role/SentinelGridRemediationWorkflowRole`), no other stack touched. One live Step Functions execution was started (Test A) — an authorized, minimal, controlled validation action per this step's explicit Phase 10 instructions, not an unrelated mutation.

## 18. Updated SRS traceability status for FR-5.4

Changed from **NOT DEMONSTRATED** (Step 50) to **PARTIALLY DEMONSTRATED**. Not escalated to full `DEMONSTRATED`, per this step's explicit instruction not to do so automatically: the retry mechanism is now genuinely implemented, deployed, and exhaustively validated locally, plus confirmed valid and non-interfering with the success path live — but the core "retry an actually-failing recovery attempt" behavior (Tests B/C) was never observed live, only locally. See `docs/srs-traceability.md` and `docs/step50_srs_traceability_results.json` for the updated record.

## 19. Explicit limitations

- Live retry/escalation behavior (Tests B/C) was not demonstrated — only locally, exhaustively. This is a genuine gap, not a testing oversight: the current deterministic simulation architecture has no natural live failure trigger for a real automatic plan.
- `RecoverySuccessCount` can over-count relative to final outcome under a verification-triggered retry sequence (§12) — a real, documented, unfixed metric-semantics limitation.
- Planner failures remain outside any retry/escalation handling — a pre-existing gap, unaddressed (§6).
- The recovery-history idempotency analysis (§13) is a reasoned, evidence-based conclusion about the CURRENT deterministic executor, not a structural guarantee that would hold if the executor ever became non-deterministic or stateful.
- `max_retries` bounds (0–5, default 2) are values chosen and justified in this document, not independently specified by the SRS itself (which only says "configurable maximum" without naming a number).

## 20. Recommended next engineering step

If a future step is authorized to modify Lambda application code (out of scope here): consider decoupling `RecoverySuccessCount`'s semantics from per-attempt Executor success toward a per-execution final-outcome metric, to eliminate the over-counting limitation in §12. Separately, if Planner-failure handling is ever required, that would need its own dedicated `Catch` and design pass, distinct from FR-5.4's recovery-execution retry scope.
