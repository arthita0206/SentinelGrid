# Remediation trigger (local implementation only — NOT deployed)

## Purpose

The missing architectural bridge, per Step 20:

```
Anomaly Detector -> DetectionResult -> [ ??? ] -> Step Functions execution
```

This package is the **decision layer** for that bridge: given a
`DetectionResult` (exactly what `detect_telemetry()` already produces —
see `lambda_functions/anomaly_detector/detector.py`), it decides whether
the deployed remediation workflow (`SentinelGridRemediationWorkflow`,
Step 18/19) should be started, and if so, computes the exact payload and
a deterministic execution name.

**This step is local design and implementation only.** Nothing here is
deployed, and nothing here calls AWS Step Functions, AWS Lambda, or any
other AWS service. See "Future AWS deployment" below for the wiring this
package is designed to support later.

## Local implementation

### What exists today

- `trigger.py` — `evaluate(detection: DetectionResult) -> TriggerDecision`
  and `compute_execution_name(node_id, timestamp, fault_type) -> str`.
  Pure, deterministic, stdlib-only. **No AWS SDK import of any kind** —
  see "AWS dependency boundary" below.
- `TriggerDecision` — a small dataclass: `should_trigger`, `reason`,
  `detection_result` (the exact payload, or `None`), `execution_name`
  (or `None`). Serializable via `.to_dict()`.
- `handler.py` (added Step 21) — the AWS-facing Lambda adapter:
  `lambda_handler(event, context)` reconstructs a `DetectionResult` from
  a plain event dict (mirroring
  `lambda_functions/remediation/planner.py`'s own extraction logic),
  calls `evaluate()` unchanged, and — only when `should_trigger` is
  `True` — calls `stepfunctions:StartExecution` via `boto3`. This is the
  **only** file in this package that imports `boto3`/`botocore` or makes
  any AWS call; enforced by a standing test
  (`test_handler_module_is_the_only_file_that_imports_boto3`).

### What does NOT exist yet (deliberately, per Step 21's scope)

- **Not deployed to AWS yet in this step's authoring — see
  `infrastructure/remediation-trigger.yaml` and
  `infrastructure/deploy-remediation-trigger.sh` for the deployment this
  module is built for; check this project's actual deployment report for
  whether that deployment has since been carried out.**
- No connection from the anomaly detector Lambda to this Trigger
  Lambda — that hand-off (Step 22) is explicitly out of scope here; this
  Lambda is deployed and tested **independently**.
- No new AWS resource beyond the Trigger Lambda and its own dedicated
  execution role.

## Local trigger contract

- **Input**: a real `DetectionResult` instance (not a dict — see
  `InvalidDetectionResultError`'s docstring for why a type mismatch here
  is treated as a programmer error, not external malformed input).
- **Triggers** (`should_trigger=True`) when, and only when, `detected is
  True` AND `node_id`/`timestamp`/`fault_type` are all present. This
  includes SENSOR_DROPOUT, NETWORK_PARTITION, DATA_CORRUPTION,
  NODE_OVERLOAD at **any** severity, and INVALID_INPUT **when it has a
  usable identity** (e.g. an unsupported-subsystem value where
  `node_id`/`timestamp` are still present).
- **Does not trigger** for `detected=False`, or for any result (INVALID_
  INPUT included) missing `node_id`/`timestamp`/`fault_type` — there is
  nothing to key an execution name on, and nothing for the workflow to
  act on.
- **Deliberately does not branch on severity or fault type beyond that
  presence check.** Whether a HIGH-severity `NODE_OVERLOAD` or an
  `INVALID_INPUT` ultimately gets auto-remediated or escalated is
  entirely `RemediationEngine.plan()`'s decision (already proven live in
  Step 19) — duplicating that policy here would violate this project's
  established "the engine is the source of truth" rule.
- **The payload is the exact, unmodified `DetectionResult.to_dict()`** —
  never wrapped as `{"detection_result": {...}}`. This matches the
  deployed state machine's actual contract exactly:
  `step_functions/remediation_workflow.json`'s `CheckAnomaly` state
  branches on `$.detected` against its own raw execution input, and
  Step 19 proved live that the execution input IS the bare
  `DetectionResult`.

## Idempotency strategy

At-least-once delivery is a real risk in this architecture: AWS IoT MQTT
delivery, Lambda invocation retries, and manual replays (as this project
has itself done deliberately in earlier steps, e.g. Step 15/19's
controlled tests) can all cause the same underlying anomaly to reach a
trigger more than once.

**This step does not implement a DynamoDB idempotency table.** Instead,
it defines a contract the eventual AWS implementation can enforce for
free, using a mechanism AWS Step Functions already provides:

- `StartExecution`'s `name` parameter is itself idempotent per state
  machine — a second `StartExecution` call with a `name` matching a
  currently-running or recently-closed execution returns
  `ExecutionAlreadyExists` rather than starting a duplicate. (This
  mirrors how `lambda_functions/anomaly_detector/event_store.py` already
  leans on DynamoDB's own conditional-write semantics instead of a
  separate lock table — reuse an AWS-native idempotency primitive
  instead of building a bespoke one.)
- `compute_execution_name()` derives a **deterministic** name from
  exactly `node_id + timestamp + fault_type` (via `hashlib.sha256`, not
  a random UUID, not wall-clock time). Any duplicate delivery of the
  *same* anomaly — same node, same telemetry timestamp, same classified
  fault — always produces the *same* execution name, so AWS's own
  `ExecutionAlreadyExists` response naturally absorbs the duplicate.
- **Why `node_id + timestamp + fault_type` and not just `node_id +
  timestamp`**: within this project's actual data model,
  `detect_telemetry()` is a pure function and `SentinelGridTelemetry`
  itself is uniquely keyed by `(node_id, timestamp)` — so in practice a
  given `(node_id, timestamp)` pair can only ever produce one
  `fault_type`. Including `fault_type` anyway costs nothing and adds a
  defensive layer of specificity against any future non-standard or
  manually-constructed input where that invariant might not hold — this
  was a deliberate choice, not an oversight (see
  `tests/test_remediation_trigger.py`'s determinism tests).
- **Why not `evidence` or `severity` or `reason`**: those describe
  *diagnostic detail* about one already-identified anomaly, not a
  separate identity dimension. Two `DetectionResult`s sharing
  `node_id`/`timestamp`/`fault_type` are treated as the same anomaly even
  if their evidence differs — the conservative choice (never
  double-trigger a real corrective action) over the alternative
  (treating cosmetic differences as a "new" anomaly). Proven directly by
  `test_same_node_and_timestamp_with_different_evidence_collapses_to_same_identity`.
- **A genuinely new anomaly** (a later telemetry reading, i.e. a
  different `timestamp`) always produces a different execution name and
  is never suppressed.

## Execution identity / name strategy

```python
compute_execution_name(node_id, timestamp, fault_type)
# -> "sentinelgrid-remediation-<32 lowercase hex chars>"
```

- `hashlib.sha256` (Python standard library) over
  `node_id + "\x1f" + timestamp + "\x1f" + fault_type`, truncated to 32
  hex characters, with a fixed `sentinelgrid-remediation-` prefix.
- The ASCII Unit Separator (`\x1f`) — not `"|"` or `"-"` — joins the
  three fields specifically because it essentially never appears in real
  telemetry identifiers, avoiding any join-ambiguity a printable
  separator could introduce.
- Result length: 25 (prefix) + 32 (hash) = 57 characters, well under AWS
  Step Functions' 80-character execution-name limit, and built only from
  `[a-z0-9-]` — entirely within the allowed character set (no whitespace,
  no `< > { } [ ] ? * " # % \ ^ | ~ \` $ & , ; : /`) by construction, not
  by runtime validation.

## Local end-to-end contract behavior

`tests/test_remediation_trigger.py`'s
`test_end_to_end_medium_node_overload_produces_valid_step_functions_input`
proves the full local chain for a MEDIUM `NODE_OVERLOAD`:
`DetectionResult -> evaluate() -> TriggerDecision(should_trigger=True,
detection_result=<exact DetectionResult>, execution_name=<deterministic>)`,
and further proves that `detection_result` can be fed **directly,
unmodified** into the existing, already-live-verified
`lambda_functions/remediation/planner.py`'s `lambda_handler` — the same
shape the real deployed `PlanRemediation` Task consumes — with no
reshaping required. No real Step Functions execution is started by this
test.

## AWS dependency boundary

This package maintains a strict, tested split:

| | Pure decision logic (`trigger.py`) | AWS-facing adapter (`handler.py`) |
|---|---|---|
| AWS SDK | Never — no `boto3`/`botocore` import | The only file in this package that imports them |
| Network / I/O | Never | Exactly one call: `stepfunctions:StartExecution` |
| Determinism | Total function of its input | Same decision logic, plus one AWS side effect |

`handler.py` is the **only** place in this whole package that knows
Step Functions exists. It reconstructs a `DetectionResult` from the
incoming event, calls the unchanged `evaluate()`, and only then decides
whether to call AWS — the decision itself is never duplicated or
reimplemented in `handler.py`. This boundary is enforced by two standing
tests: `test_pure_trigger_module_has_no_aws_or_network_or_subprocess_imports`
(scoped to `trigger.py`/`__init__.py`) and
`test_handler_module_is_the_only_file_that_imports_boto3` (scoped to the
whole package, asserting `handler.py` is the sole exception).

## Future AWS deployment

### A. Architecture options considered

| Option | Description | Verdict |
|---|---|---|
| A — Detector calls Step Functions directly | Add `states:StartExecution` + boto3 call straight into `handler.py` | **Rejected** — couples detection tightly to remediation orchestration; broadens the detector's permission/blast-radius surface; this project explicitly aims for clean separation of concerns |
| B — Detector publishes to EventBridge/SNS, which targets Step Functions | New pub/sub service as an intermediate | **Rejected for now** — introduces a brand-new AWS service the project has never used, for a benefit (multi-consumer fan-out) not currently needed; also EventBridge's declarative event-pattern routing wouldn't run this package's own Python decision logic at all, making a chunk of this step's deliverable unused |
| C — A third IoT topic rule routes raw telemetry straight to Step Functions | Bypass the detector Lambda entirely | **Rejected** — would force Step Functions to either duplicate detection logic (explicitly forbidden) or start (and immediately no-op) an execution for *every* telemetry message, not just anomalies — poor cost/observability characteristics, and a bigger ASL redesign than this project's incremental steps intend |
| **D — A dedicated, separate "Remediation Trigger" Lambda** | Detector hands off only anomalous `DetectionResult`s to a new, purpose-built Lambda, which calls `StartExecution` | **Chosen** |

### B. Why Option D was selected

- **Separation of concerns**: the existing `SentinelGridAnomalyDetector`
  gains exactly one new, narrow responsibility — *forward* an anomalous
  result — not the responsibility of *deciding remediation orchestration
  outcomes*. It still never needs to know execution names, retry
  policies, or anything about Step Functions' semantics.
- **Least privilege**: the detector's execution role would gain exactly
  one new permission, `lambda:InvokeFunction` scoped to the new Trigger
  Lambda's exact ARN — nothing broader. The new Trigger Lambda gets its
  own dedicated role (never reusing the detector's or the three
  remediation Lambdas' roles, matching this project's established
  one-Lambda-one-role convention from Steps 9/17) with exactly
  `states:StartExecution` scoped to exactly
  `SentinelGridRemediationWorkflow`'s ARN.
- **Uses this package's own decision logic as designed**: `evaluate()`
  clearly presupposes something runs this Python code — a Lambda is the
  natural fit; a declarative EventBridge rule (Option B) would not run
  it at all.
- **No new AWS service**: reuses the same "small, dedicated Lambda"
  pattern this project already uses four times over (detector, planner,
  executor, verifier) rather than introducing EventBridge/SNS purely for
  this bridge.
- **Preserves existing behavior (Task 2)**: the detector's existing
  `detect_telemetry() -> _log_result() -> _persist_if_anomalous()` flow
  is untouched; the new hand-off would be strictly additive, most safely
  as an **asynchronous** (`InvocationType="Event"`), best-effort
  Lambda-to-Lambda invocation placed *after* persistence — so a
  Trigger-Lambda-invoke failure can never affect the detector's own
  fail-fast persistence guarantee (see
  `lambda_functions/anomaly_detector/README.md`'s persistence-failure
  policy), and async invocation gives free built-in retry (2 automatic
  retries) without any extra code.

### C. What `handler.py` implements (built in Step 21)

1. Receive an event shaped exactly like `DetectionResult.to_dict()`
   (the detector will eventually invoke it with that payload — see
   Step 22, not yet wired up).
2. Reconstruct a `DetectionResult` from it (a thin adapter, mirroring
   `lambda_functions/remediation/planner.py`'s existing
   `_extract_detection_result` pattern) and call this package's
   `evaluate()` — no detection or remediation policy duplicated.
3. If `should_trigger`, call
   `stepfunctions:StartExecution(stateMachineArn=..., name=decision.execution_name, input=json.dumps(decision.detection_result))`.
4. Catch `ExecutionAlreadyExists` and treat it as a **successful,
   idempotent no-op** — log it distinctly, do not raise — mirroring
   exactly how `event_store.py` already treats a
   `ConditionalCheckFailedException` as success, not failure.
5. Any *other* `StartExecution` failure propagates (fail loud, matching
   this project's fail-fast convention) so it surfaces in CloudWatch and
   the async invocation's own retry/DLQ handling.

### D. IAM permissions needed (not created yet)

- **Detector's execution role** gains: `lambda:InvokeFunction`, scoped
  to exactly the new Trigger Lambda's ARN.
- **New Trigger Lambda's own execution role**: trust `lambda.amazonaws.com`
  only; CloudWatch Logs (standard, scoped to its own log group, matching
  every other Lambda role in this project); exactly
  `states:StartExecution`, scoped to exactly
  `SentinelGridRemediationWorkflow`'s ARN — no `states:*`, no wildcard.

### E. Duplicate delivery / failure handling (future)

- **Duplicate delivery** (same anomaly delivered twice): both attempts
  compute the same `execution_name`; the second `StartExecution` call
  gets `ExecutionAlreadyExists` and is treated as success (see above).
- **`StartExecution` fails for another reason** (e.g. throttling, IAM
  misconfiguration): the error propagates out of the Trigger Lambda.
  Because the detector invokes it asynchronously, Lambda's own
  automatic retry (2 attempts) handles transient failures for free; a
  future step could add an on-failure destination (e.g. an SQS DLQ) for
  durable observability of anything that still fails after retries —
  not built in this step.
- **Retries**: exactly Lambda's built-in async-invoke retry behavior —
  no custom retry loop needed or added.
- **Observability**: each Trigger Lambda invocation's CloudWatch logs
  record the `TriggerDecision` (via `.to_dict()`, already
  JSON-serializable) and the resulting `StartExecution` outcome
  (started / already-exists / failed), giving a clear per-anomaly audit
  trail without any new AWS service.

None of this AWS infrastructure — the Trigger Lambda itself, its role,
the detector's new invoke permission, or any CloudFormation template —
is created by this step. It is design-and-local-implementation only.
