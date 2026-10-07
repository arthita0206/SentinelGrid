# SentinelGrid Fault and Resilience Validation (Step 46)

## 1. Objective

Controlled, end-to-end validation of the already-implemented detection
and remediation pipeline against SRS FR-4 (detection), FR-5 (automatic
recovery/escalation), FR-6 (persistence/query), and FR-7 (metrics). This
step is about **faults**, not scale — Step 38 already validated normal
500-node telemetry throughput. Every result below is measured against an
independently computed **ground truth**, never assumed.

## 2. Test environment

Same AWS account (`401429382000`), region (`ap-south-1`), and profile
(`sentinelgrid`) as Steps 36–38. Lambda account concurrency: **400**
(unchanged since Step 38). No infrastructure was modified for this step.

## 3. What was inspected before testing (Phase 1)

- `fault_injection/faults.py`/`engine.py`/`corruption.py`/`overload.py` —
  the existing fault injection engine. Not modified; not replaced.
- `lambda_functions/anomaly_detector/detector.py` — the deployed
  detection rules, read in full.
- `remediation/engine.py` — the deployed `RemediationEngine`/
  `RemediationExecutor` mapping (matches the task's own description
  exactly: SENSOR_DROPOUT/NETWORK_PARTITION/DATA_CORRUPTION always
  automatic; NODE_OVERLOAD automatic only at MEDIUM; INVALID_INPUT
  always escalation).
- `lambda_functions/remediation/planner.py` — confirmed the Planner
  itself (not the Executor/Verifier) owns escalation side-effects:
  `UnresolvedFaultCount`, the `RecoveryEvents` escalation record, and
  `NodeState=ESCALATION_REQUIRED`.
- `shared/node_state_store.py` — confirmed a brand-new node_id's first
  write is never rejected by the ordering `ConditionExpression`
  (`attribute_not_exists(#pk) OR ...`).
- `lambda_functions/remediation_trigger/handler.py` — confirmed its
  exact input contract (`DetectionResult.to_dict()`) and that it calls
  the real, unmodified `stepfunctions:StartExecution` with that input
  unwrapped — the same call the real Analyzer makes for a genuine
  detection.

**Two findings from this inspection directly shaped the test design**
(see §7 for the full detail): (1) `NODE_OVERLOAD` can only ever be
classified `MEDIUM` by the deployed detector — there is no telemetry
value that produces `HIGH`/`CRITICAL`; (2) `SensorDropoutFault` and
`NetworkPartitionFault` both simply suppress publishing — no telemetry
reaches the pipeline, so neither fault type can be exercised end-to-end
via the existing fault injection mechanism at all.

## 4. Baseline (Phase 2)

| | Before | After | Delta |
|---|---|---|---|
| SQS visible / not-visible | 0 / 0 | 0 / 0 | 0 |
| DLQ | 0 | 0 | 0 |
| `SentinelGridNodeState` | 1,137 | 1,142 | +5 |
| `SentinelGridAnomalyEvents` | 17 | 21 | +4 |
| `SentinelGridRecoveryEvents` | 16 | 21 | +5 |

Deltas match the test cohort exactly (5 nodes, 4 real detections, 5
remediation outcomes — see §6).

## 5. Test design (Phase 3)

Five isolated, single-node live test cases (plus two documented
limitations), each with a unique `node_id`, run sequentially with a full
drain-and-confirm wait between cases (Phase 4 isolation). A new script,
`scripts/step46_fault_test.py`, reuses the EXISTING `CitySimulator`,
`FaultInjectionEngine`, and `AwsIotPublisher` classes unchanged — it
builds a 1-node config for one subsystem, injects one fault via
`engine.inject()` (the existing, documented manual-injection API), and
publishes through the real IoT path. It also independently computes
**local ground truth** by calling the exact same
`lambda_functions.anomaly_detector.detector.detect_telemetry()` function
the deployed Lambda runs, on the exact payload published — an
independent, code-derived expected value, not a guess.

Test Case B uses a second small script,
`scripts/step46_escalation_control_plane_test.py` — see §7.2.

## 6. Test matrix

| Test | Ground Truth | Expected Detection | Observed Detection | Expected Remediation | Observed Remediation | Final State | Result |
|---|---|---|---|---|---|---|---|
| A (overload, 3-tick) | NODE_OVERLOAD | MEDIUM, automatic | NODE_OVERLOAD MEDIUM, detected | REDUCE_LOAD, automatic | REDUCE_LOAD, success=true | HEALTHY* | PASS |
| A2 (overload, 1-tick) | NODE_OVERLOAD | MEDIUM, automatic | NODE_OVERLOAD MEDIUM, detected | REDUCE_LOAD, automatic | REDUCE_LOAD, success=true | **RECOVERED** | PASS |
| B (control-plane) | NODE_OVERLOAD HIGH (synthetic) | escalation | escalation (Planner only) | none (escalate) | REDUCE_LOAD planned, not executed | ESCALATION_REQUIRED | PASS |
| C (corruption, 3-tick) | DATA_CORRUPTION | HIGH, automatic | DATA_CORRUPTION HIGH, detected | RESET_SENSOR_STATE, automatic | RESET_SENSOR_STATE, success=true | HEALTHY* | PASS |
| C2 (corruption, 1-tick) | DATA_CORRUPTION | HIGH, automatic | DATA_CORRUPTION HIGH, detected | RESET_SENSOR_STATE, automatic | RESET_SENSOR_STATE, success=true | **RECOVERED** | PASS |
| D (dropout) | SENSOR_DROPOUT | — | not exercisable | — | — | — | NOT TESTED (limitation) |
| E (partition) | NETWORK_PARTITION | — | not exercisable | — | — | — | NOT TESTED (limitation) |

\* See §7.1 — HEALTHY, not a failure, is an artifact of the 3-tick test
design (2 subsequent NORMAL ticks superseded the RECOVERED state). The
single-tick supplementary runs (A2, C2) confirm RECOVERED directly.

## 7. Notable findings during testing

### 7.1 NodeState reflects the LATEST telemetry, not history

Test Cases A and C each published 3 ticks (fault on tick 1, NORMAL on
ticks 2–3), matching the task's initial per-case tick guidance. This
revealed that `SentinelGridNodeState` — by design, correctly — reflects
whatever telemetry arrived most recently, so the genuine `RECOVERED`
transition (confirmed by each case's own `RecoveryEvents` record,
`success=true`) was overwritten back to `HEALTHY` once ticks 2–3's
normal telemetry was processed. This is **not a system defect** — it is
the expected, correct behavior of a "current state" table. It is,
however, a real limitation of a multi-tick test design for observing a
transient state. Rather than report a misleading "final state HEALTHY,
expected RECOVERED, FAIL," two supplementary single-tick runs (A2, C2,
fresh unique node IDs) were performed specifically to observe the
uncounfounded `RECOVERED` transition — both confirmed it directly. Both
the 3-tick and 1-tick results are recorded in the evidence JSON exactly
as observed, not adjusted after the fact.

### 7.2 Test Case B is a control-plane validation, not an IoT test

Direct inspection of `detector.py`'s `NUMERIC_FIELD_SPECS` shows the
"overload" band for every subsystem's overloadable field always yields
exactly `Severity.MEDIUM` — there is no numeric value
`fault_injection/overload.py` could ever produce, and no detector rule,
that classifies as `NODE_OVERLOAD` at `HIGH`/`CRITICAL`. Per Step 46's
explicit instruction not to invent a new methodology to force this, Test
Case B instead directly invoked the **existing, deployed**
`SentinelGridRemediationTrigger` Lambda with a hand-crafted
`DetectionResult` (`fault_type=NODE_OVERLOAD, severity=HIGH`) — the
exact same wire shape and Lambda the real Analyzer already calls for a
genuine detection. This exercised the real
`Planner -> CheckAutomaticRemediation -> Escalate` path end-to-end;
confirmed via `GetExecutionHistory` that only `CheckAnomaly`,
`PrepareRemediationPlanning`, `PlanRemediation`,
`CheckAutomaticRemediation`, and `Escalate` were entered — **no**
`ExecuteRemediation`/`VerifyRemediation` state, confirming Executor and
Verifier were never invoked. Because the Analyzer was bypassed,
`SentinelGridAnomalyEvents` correctly has **zero** records for this
test's node — this is expected, not a defect, and is reported exactly
as such. This result validates the escalation control logic
end-to-end; it says nothing about whether real telemetry can ever
reach the detector's HIGH-severity NODE_OVERLOAD path, because it
cannot.

### 7.3 SENSOR_DROPOUT and NETWORK_PARTITION were not exercisable

Both `SensorDropoutFault.blocks_publishing()` and
`NetworkPartitionFault.blocks_publishing()` simply suppress publishing
for their target node — no telemetry of any kind reaches SQS, the
Consumer, or the Analyzer, so the Lambda pipeline is never invoked and
no detection is possible via these fault types as currently
implemented. `detector.py` does contain rules that WOULD classify an
explicit in-band signal as `SENSOR_DROPOUT` (`status != 'ONLINE'`, or
missing/empty `data`) or as `NETWORK_PARTITION` (an explicit
`network_status` field set to `PARTITIONED`/`UNREACHABLE`) — but
neither fault type's current implementation, nor any other code in
`simulator/`, ever produces such a signal. `detector.py`'s own comment
on the network-partition hook says so explicitly: "today's simulator
never sets it, so this path is not exercised by real output." Per Step
46's explicit instruction, no new heartbeat service, stateful detector,
or fault-injection signal was built to force these two cases to be
testable. **Missing-heartbeat/absence-based detection was not
exercised because the deployed pipeline has no stateful heartbeat-
timeout mechanism, and the existing fault injection mechanism for
these two fault types produces no in-band signal the stateless,
per-message detector could act on.**

## 8. Detection accuracy (Phase 5)

- Ground-truth fault cases via real telemetry: **4** (A, A2, C, C2 —
  Case B is a control-plane test, excluded from this count)
- Detected: 4/4
- Correctly classified (fault type): 4/4
- Correctly classified (severity): 4/4
- Not detected: 0
- Duplicate detections: 0

**n=4 is a small, controlled functional sample. This demonstrates
correct functional behavior for these specific cases — it is NOT a
statistically meaningful detection-accuracy measurement, and no
percentage accuracy claim is made from it.**

## 9. False positives (Phase 6)

A full scan of `SentinelGridAnomalyEvents` for the entire test window
(2026-09-16T11:08–11:13 UTC) found exactly 4 records, all matching the
expected test cohort — **0 unexpected anomalies** from any other node.

**False-positive rate not statistically demonstrated** — this is one
~5-minute window with a 4-node live cohort, not a sustained or
large-population observation. It is consistent with, but not additive
to, Step 38's separate 500-node normal-telemetry evidence (0 anomalies
across 6,000 normal readings).

## 10. Automatic remediation (Phase 7)

- Attempts: 4 (A, A2, C, C2)
- Successes: 4/4
- Failures: 0
- Escalations: 1 (B)
- Duplicate executions: 0 (verified — exactly 5 Step Functions
  executions in the test window, one per test node, each `SUCCEEDED`
  exactly once)

For each automatic case (A, A2, C, C2): detection occurred, fault type
was correct, the remediation plan matched the known FR-5 mapping,
`automatic=true` was correct, a Step Functions execution started,
Executor ran, Verifier ran, recovery succeeded, the recovery event was
persisted, and NodeState ended `RECOVERED` (A2/C2 directly; A/C via
their own `RecoveryEvents` record, see §7.1).

For the escalation case (B): the Planner ran, `automatic=false` was
correct, Executor and Verifier did NOT run (confirmed via execution
history), `ESCALATION_REQUIRED` was persisted to `RecoveryEvents`,
NodeState became `ESCALATION_REQUIRED`, and `UnresolvedFaultCount` was
incremented by exactly 1 for the `power`/`NODE_OVERLOAD` dimension pair.

## 11. Latency (Phase 8)

Using the existing CloudWatch `DetectionLatency`/`RecoveryLatency`
metrics (the same measurement this project has used since Step 28/36 —
no new latency definition was invented) and Step Functions'
`startDate`/`stopDate` for workflow duration:

| Case | Detection latency | Recovery latency | Workflow duration |
|---|---|---|---|
| A | 5,468 ms | 18,073 ms | 8.062 s |
| A2 | 1,227 ms | 2,716 ms | 1.007 s |
| C | 1,907 ms | 5,629 ms | 0.691 s |
| C2 | 940 ms | 1,904 ms | 0.684 s |

All four are well under NFR-1's 10-second detection target and NFR-2's
30-second recovery target. **This is observed in this controlled
sample (n=4) — it is not a statistical demonstration of NFR-1/NFR-2
compliance**, which requires a much larger sample than this step's
scope (see Step 36's own n=10 result and this same caveat).

## 12. CloudWatch validation (Phase 9)

All existing metrics, unmodified (`SentinelGrid` namespace,
`Subsystem`/`FaultType` dimensions only — no new metric was added):

| Metric | `power`/`NODE_OVERLOAD` | `power`/`DATA_CORRUPTION` |
|---|---|---|
| FaultDetectionCount | 2 (A, A2) | 2 (C, C2) |
| RecoveryAttemptCount | 2 | 2 |
| RecoverySuccessCount | 2 | 2 |
| RecoveryFailureCount | 0 | 0 |
| UnresolvedFaultCount | 1 (B) | 0 |

Every increment matches the live test cohort exactly — `RecoveryFailureCount`
is genuinely 0 because no failure path was exercised in this step (not
claimed as "the system never fails," only that it was not observed
here).

## 13. Persistence (Phase 10)

Verified directly via `dynamodb:GetItem`/`dynamodb:Query` for every test
node — see the full raw records in
`docs/step46_fault_validation_results.json`. `SentinelGridTelemetry` was
not separately re-verified in this step (Step 37/38 already established
the telemetry-persistence path in detail); the anomaly/recovery/
NodeState records for all 5 nodes are confirmed correct in §6–§7.

## 14. Pipeline/queue regression (Phase 11)

- SQS: 0 visible, 0 not-visible (final)
- DLQ: 0
- `SentinelGridSqsTelemetryConsumer` / `SentinelGridAnomalyDetector` /
  `SentinelGridRemediationTrigger`: 0 Errors, 0 Throttles across the
  entire test window
- Exactly 5 Step Functions executions in the window, all `SUCCEEDED`, no
  unexplained executions

## 15. NFR assessment (Phase 10 / overall)

**NFR-1 (Detection ≥95% within 10s)**: Functionally demonstrated for 4
individual cases (all well under 10s: 940ms–5,468ms). **Not
statistically demonstrated** — n=4 cannot support a 95% claim. Combined
with Step 36's own n=10 result (9/10 ≤10s), this project still has no
sample large enough to statistically claim NFR-1.

**NFR-2 (Recovery ≤30s for 90% of applicable recoveries)**:
Functionally demonstrated for 4 individual automatic-recovery cases (all
well under 30s: 1,904ms–18,073ms) and the one escalation case correctly
did NOT attempt an automatic recovery (per its own policy). **Not
statistically demonstrated** — same n=4 limitation.

**NFR-3 (support up to 500 virtual nodes)**: Unchanged from Step 38 —
this step does not add scale evidence; it adds fault-path *correctness*
evidence at small scale. Step 38 remains the scale evidence; Step 46 is
the fault-correctness evidence. Together they show the pipeline handles
both a 500-node normal-telemetry burst AND individual real fault
scenarios correctly, but the two have not been tested simultaneously
(faults injected concurrently with a 500-node normal load) — that
combination remains untested.

## 16. Limitations

- SENSOR_DROPOUT and NETWORK_PARTITION could not be exercised end-to-end
  via the existing fault injection mechanism (§7.3) — this is a genuine
  gap in what this step could validate, not a claim that these fault
  types don't work; it means their detection paths are currently
  unreachable by any real telemetry the system can produce.
- Test Case B is a control-plane validation only, explicitly not
  evidence about real IoT-to-HIGH-severity-NODE_OVERLOAD detection
  (which is provably impossible under current detector rules).
- Sample sizes (n=4 detections, n=1 escalation) are too small for any
  statistical NFR claim — only functional/correctness claims are made.
- Cases A/C's "final NodeState" result reflects a property of a 3-tick
  test design, not a system defect (§7.1) — corrected via supplementary
  single-tick runs A2/C2.
- No fault was tested concurrently with sustained normal traffic; Step
  38's 500-node load and this step's fault cases were run at separate
  times.

## 17. Files created

- `scripts/step46_fault_test.py` — new live fault-test driver (reuses
  existing `CitySimulator`/`FaultInjectionEngine`/`AwsIotPublisher`
  unchanged; no new fault-injection framework).
- `scripts/step46_escalation_control_plane_test.py` — new control-plane
  escalation test driver (direct, single invocation of the existing
  `SentinelGridRemediationTrigger` Lambda).
- `docs/step46_fault_validation_results.json` — structured evidence for
  every test case.
- `docs/step46_fault_validation.md` — this report.
- `docs/step46_caseA_events.json`, `docs/step46_caseA2_events.json`,
  `docs/step46_caseB_events.json`, `docs/step46_caseC_events.json`,
  `docs/step46_caseC2_events.json` — raw per-case run output from the
  test scripts.

**No application/Lambda/infrastructure code was modified in this
step.**
