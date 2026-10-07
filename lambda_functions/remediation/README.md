# Remediation Lambda adapters (local implementation only — NOT deployed)

## Purpose

Three thin, AWS-Lambda-compatible adapters around the existing local
remediation engine (`remediation/engine.py`), mirroring how
`lambda_functions/anomaly_detector/handler.py` is a thin adapter around
`lambda_functions/anomaly_detector/detector.py`:

```
DetectionResult
      |
      v
planner.lambda_handler()     -> RemediationEngine.plan()
      |
      v
RemediationPlan
      |
      v
executor.lambda_handler()    -> RemediationExecutor.execute()
      |
      v
RemediationResult
      |
      v
verifier.lambda_handler()    -> inspects (never re-executes) the result
      |
      v
VerificationResult
```

**Step 16/16A status: local implementation and tests only.** None of these
three functions has been created in AWS, packaged, or deployed. No
CloudFormation, IAM role, or S3 deployment artifact exists for them yet
— that is explicitly a separate, later step. Nothing here makes an AWS
SDK call, a network call, or touches the filesystem; all three modules
are pure functions of their `event` argument, enforced by a standing AST
regression test (see `tests/test_remediation_lambdas.py`).

## Relationship to `remediation/engine.py`

**`remediation/engine.py` remains the sole source of truth for
remediation policy.** None of these three adapters reimplements any
fault-to-action mapping, severity-gating, or escalation rule — each one
only extracts a plain dict into the matching dataclass, calls exactly one
existing method (`RemediationEngine.plan()` or
`RemediationExecutor.execute()`), and serializes the result back to a
dict. `remediation/models.py`, `remediation/engine.py`, and
`remediation/integration.py` were **not modified** by this step.

One deliberate design point worth calling out: when `executor.py`
reconstructs a `RemediationPlan` from JSON and encounters an
`action_type` value that isn't a recognized `ActionType` member, it does
**not** reject it itself — it passes the raw value through unchanged and
lets `RemediationExecutor.execute()`'s own existing defensive check (see
`remediation/engine.py`) reject it via `ExecutionStatus.REJECTED`.
Re-validating that here would duplicate policy this adapter doesn't own.

## Contract (Step 16A — corrected from Step 16's accumulated-envelope design)

**Step Functions owns workflow-state accumulation. Each Lambda returns
only its own stage's result.** This replaces Step 16's original design,
where each handler returned the full accumulated envelope — that design
was discovered to be incompatible with
`step_functions/remediation_workflow.json`'s existing `ResultPath`
values (see "Step Functions contract" below for the full accumulation
mechanics and one remaining, separate mismatch this step does not fix).

| Stage | Input | Output (success) |
|---|---|---|
| Planner | `DetectionResult` | `RemediationPlan` (only) |
| Executor | `{"detection_result": ..., "remediation_plan": ...}` | `RemediationResult` (only) |
| Verifier | `{"detection_result": ..., "remediation_plan": ..., "remediation_result": ...}` | `{"verified": bool, "reason": str}` (only) |

## Planner contract (`planner.py`)

- **Input**: a `DetectionResult`-shaped dict (the exact 8-field shape
  `lambda_functions/anomaly_detector/detector.py`'s `DetectionResult.
  to_dict()` produces — `detected`, `fault_type`, `severity`, `node_id`,
  `subsystem`, `timestamp`, `reason`, `evidence`).
- **Output (success)**: the serialized `RemediationPlan`
  (`RemediationPlan.to_dict()`) directly — **not** wrapped in
  `{"detection_result": ..., "remediation_plan": ...}`.
- **Output (malformed input)**: `{"error": "invalid_detection_result", "message": "..."}`.
- Never calls `detect_telemetry()` again, and never imports or calls
  `event_store.py` — it has no DynamoDB dependency at all.

## Executor contract (`executor.py`)

- **Input**: `{"detection_result": {...}, "remediation_plan": {...}}` —
  the Step-Functions-accumulated state expected after `PlanRemediation`.
- **Output (success)**: the serialized `RemediationResult`
  (`RemediationResult.to_dict()`) directly — **not** wrapped with
  `detection_result`/`remediation_plan`. This is always safe to build via
  `.to_dict()`: `RemediationExecutor.execute()` guarantees a real
  `ActionType` in every branch it returns, including `REJECTED`, even
  though the *input* plan's `action_type` may have been an unrecognized
  raw value (see the design point above).
- **Output (malformed input)**: `{"error": "invalid_remediation_plan", "message": "..."}`.
- The existing `RemediationExecutor` remains exactly what it already
  was — a safe, local, deterministic **simulation**. This adapter does
  not turn it into a real infrastructure actuator, and preserves its
  escalation behavior exactly: an `automatic=False`/HIGH/CRITICAL
  overload/`INVALID_INPUT` plan still produces `success=False,
  status=ESCALATED` here, never a fabricated success.

## Verifier contract (`verifier.py`)

- **Input**: `{"detection_result": {...}, "remediation_plan": {...}, "remediation_result": {...}}`
  — the Step-Functions-accumulated state expected after
  `ExecuteRemediation`.
- **Output**: always exactly `{"verified": bool, "reason": str}` —
  **not** wrapped with `detection_result`/`remediation_plan`/
  `remediation_result` — **including on malformed input** (see
  "Error-handling strategy" below).
- `verified` is `True` only when `remediation_result.success is True`,
  `remediation_result.status == "SUCCESS"`, and the result's `action`/
  `node_id`/`subsystem`/`fault_type` all correspond to the plan's. Any
  escalated/failed remediation, any malformed/missing `remediation_result`,
  and any plan/result mismatch all produce `verified=False` with a
  specific `reason` — never a fabricated success.
- Only **inspects** `remediation_result` — never modifies it, and never
  queries AWS, DynamoDB, IoT, or a real sensor. There is no real-world
  health check here; this is a safe simulation of the verification
  *shape* the workflow needs, not a real check.

## Error-handling strategy (deliberately asymmetric, and why)

All three handlers prefer a structured, JSON-serializable response over
letting an exception escape — consistent with this project's existing
convention (`detect_telemetry()`/`lambda_handler` in
`lambda_functions/anomaly_detector/` never raise on malformed input
either; they return a structured `INVALID_INPUT` result instead).

- **Planner and executor** return a distinct `{"error": "...", "message": "..."}`
  envelope on malformed input — there is no way to produce a meaningful
  `RemediationPlan`/`RemediationResult` from an unusable
  `DetectionResult`/`RemediationPlan`, so the response shape
  intentionally signals "this stage could not run" rather than
  fabricating a plan/result.
- **The verifier never uses that error envelope.** It always returns
  exactly `{"verified": bool, "reason": str}` (`verified=False` on
  malformed input, with a clear reason) — the same shape as its normal
  success/failure output, never a distinct error shape. This is
  deliberate: `verified` is exactly the boolean a Step Functions
  `Choice` state would branch on (see
  `step_functions/remediation_workflow.json`'s `CheckVerification`
  state, via `$.verification.verified` once Step Functions merges this
  return value in) — a distinct error shape there would give a workflow
  nothing to route on. The verifier is the last stage before
  Success/Escalate, so it must always answer the one question it exists
  to answer.

No handler silently fills in a missing required field to avoid an
error — a missing `node_id`/`automatic`/`action`/etc. is treated as
genuinely invalid input, not defaulted.

## Safety constraints (all enforced, all tested)

- No `boto3`/`botocore`/AWS SDK import anywhere in this package —
  enforced by a standing AST-based test.
- No network calls (`socket`, `urllib`, `http`, `requests`) and no
  `subprocess` calls — enforced by the same test.
- No filesystem writes — enforced by a dedicated AST test that rejects
  any `open(..., "w"/"a"/...)` call.
- No module-level mutable global state (no shared list/dict/set that
  could leak between invocations) — enforced by a dedicated AST test.
  Unlike `lambda_functions/anomaly_detector/handler.py`'s module-level
  `_event_store` singleton (which exists specifically to lazily hold a
  boto3 client across warm Lambda invocations), these adapters call
  nothing external, so they have no equivalent need.
- No randomness, no wall-clock timestamps generated anywhere in this
  package — every timestamp in the output is carried through from the
  input, never generated fresh.
- Every handler's input `event` is read-only from that handler's
  perspective — proven by dedicated tests that deep-copy the input
  before calling and assert it is byte-for-byte unchanged afterward.

## Step Functions contract (fully resolved as of Step 16B)

Step Functions owns workflow-state accumulation through each Task's
`InputPath`/`ResultPath` in `step_functions/remediation_workflow.json`;
the Lambda adapters only ever return their own stage-specific output:

- Step Functions passes the `DetectionResult` to the Planner using
  `InputPath: "$.detection_result"`.
- The Planner's output is stored at `$.remediation_plan`
  (`ResultPath: "$.remediation_plan"`).
- Step Functions passes the **complete accumulated state**
  (`{"detection_result": ..., "remediation_plan": ...}` at this point)
  to the Executor using `InputPath: "$"`.
- The Executor reads `detection_result`/`remediation_plan` out of that
  state and returns only a `RemediationResult`.
- Step Functions stores that output at `$.remediation_result`
  (`ResultPath: "$.remediation_result"`).
- Step Functions passes the complete accumulated state (now including
  `remediation_result`) to the Verifier, also using `InputPath: "$"`.
- The Verifier returns only the verification object.
- Step Functions stores it at `$.verification`
  (`ResultPath: "$.verification"`).

```
PlanRemediation:    InputPath == "$.detection_result"   ResultPath == "$.remediation_plan"
ExecuteRemediation: InputPath == "$"                     ResultPath == "$.remediation_result"
VerifyRemediation:  InputPath == "$"                     ResultPath == "$.verification"
```

Given that, Step Functions accumulates the workflow state like this,
purely through those `InputPath`/`ResultPath` mechanics — no Lambda
needs to (or should) reconstruct this shape itself:

```
{}                                                     (initial)
{"detection_result": {...}}                            (after PrepareRemediationPlanning)
{"detection_result": {...}, "remediation_plan": {...}}  (after PlanRemediation)
{"detection_result": {...}, "remediation_plan": {...}, "remediation_result": {...}}  (after ExecuteRemediation)
{"detection_result": {...}, "remediation_plan": {...}, "remediation_result": {...}, "verification": {...}}  (after VerifyRemediation)
```

This is a two-part fix across Step 16A and Step 16B:

- **Step 16A** corrected the *output* side: each Lambda now returns only
  its own incremental result (a `RemediationPlan`, a `RemediationResult`,
  or a verification object) rather than an accumulated envelope, so each
  `ResultPath` merge produces the shape above without nesting.
- **Step 16B** corrected the *input* side: `ExecuteRemediation`'s
  `InputPath` was `"$.remediation_plan"` (sending only the bare plan,
  which didn't match the executor's documented
  `{"detection_result": ..., "remediation_plan": ...}` input contract)
  and has been changed to `"$"`, so the executor now receives the
  complete accumulated state, exactly like the verifier already did.

Both are proven directly by `tests/test_remediation_lambdas.py`:
`test_step_functions_inputpath_and_resultpath_match_asl_exactly` reads
the six actual `InputPath`/`ResultPath` values from the ASL file (not
duplicated/assumed ones);
`test_step_functions_resultpath_accumulation_produces_correct_shape_no_nesting`
(plus its SENSOR_DROPOUT/HIGH NODE_OVERLOAD/INVALID_INPUT variants)
models the full accumulation and confirms `$.remediation_plan.automatic`,
`$.remediation_result.status`, and `$.verification.verified` — the exact
paths `CheckAutomaticRemediation`/`CheckVerification` rely on — all
resolve correctly with no accidental self-nesting; and
`test_executor_receives_complete_accumulated_state_via_inputpath_dollar`
is the regression test proving the `InputPath` fix itself.

**Note on field naming:** `RemediationResult`'s outcome field is named
`status` (an `ExecutionStatus` value — see `remediation/models.py`) —
there is no `execution_status` field, and none was added for this step.

## Future deployment architecture (not built yet)

Once reviewed, a later step will: package each of the three handlers similarly to
`infrastructure/package-anomaly-detector.sh` (its own new, separate
packaging script — explicitly not sharing or modifying that existing
script); create a new CloudFormation template defining the three Lambda
functions and their (still-to-be-designed, least-privilege) execution
roles; and only then update `infrastructure/remediation-workflow.yaml`
to supply real Lambda ARNs via `DefinitionSubstitutions`, resolving the
placeholder Task `Resource` values that blocked deployment in Step 15.
None of that exists yet.

## Recovery-event history (Step 33)

`lambda_functions/remediation/recovery_event_store.py` persists recovery
and escalation OUTCOMES to the `SentinelGridRecoveryEvents` DynamoDB
table (`infrastructure/recovery-events-storage.yaml`) — this is
historical recovery-**event** storage, not a current-node-state model
(see infrastructure/README.md for the full schema/semantics). It mirrors
`lambda_functions/anomaly_detector/event_store.py`'s own pattern exactly
(lazy boto3, `table=` injection, Decimal-safe conversion, conditional
idempotent `PutItem`, fail-fast on real failures) and is the one module
in this package that imports boto3 — `remediation/models.py` and
`remediation/engine.py` remain completely AWS-independent, unchanged by
this step.

**Ownership** (mutually exclusive per event, exactly like Step 28's
`UnresolvedFaultCount` metric split): `planner.py` persists an
`ESCALATION_REQUIRED` event when it decides `escalation_required=True`;
`executor.py` persists a `SUCCESS`/`FAILURE`/`ESCALATION_REQUIRED` event
(mapped from `ExecutionStatus`) for every plan it receives, except
`SKIPPED` (nothing to record). Neither `RemediationPlan.to_dict()` nor
`RemediationResult.to_dict()`'s external response shape changed to
accommodate this — persistence is a side effect, not a new response
field.

**The Step Functions ASL was not modified.** Both writers build the
recovery event entirely from information `planner.py`/`executor.py`
already had in scope; `execution_name`/`execution_arn` are therefore NOT
persisted (they are only available via the Step Functions Context
Object, which would require an ASL change this step deliberately avoids)
— a documented, deliberate limitation, not an oversight.
