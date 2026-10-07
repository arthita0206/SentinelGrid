# SentinelGrid Performance Measurement & Validation (Step 36)

## 1. Objective

Step 36 builds a rigorous, reproducible measurement foundation for
SentinelGrid's performance claims. It does **not** optimize the system.
Its purpose is to answer, with evidence:

1. What exactly does `DetectionLatency` measure?
2. What exact timestamps are involved?
3. Can the latency measurement be reproduced independently?
4. Where is latency introduced?
5. What is the actual end-to-end telemetry-to-detection timing?
6. What is the actual detection-to-remediation timing?
7. What is the actual remediation workflow duration?
8. Can these phases be measured separately and cleanly?
9. Can statistically meaningful (multi-sample) measurements be produced,
   rather than relying on one event (as Step 29 did)?
10. What can honestly be said about NFR-1 and NFR-2 after measurement?

## 2. SRS requirements being evaluated

- **NFR-1**: Detection should occur within 10 seconds, target ≥95% of cases.
- **NFR-2**: Recovery should complete within 30 seconds, target ≥90% of cases.
- **NFR-3**: System should support 500 virtual nodes. **Not evaluated in
  Step 36** — this step used a 10-event sample, not a 500-node load test.

## 3. Measurement definitions

### 3.1 What the existing metrics actually measure (Phase A finding)

Both existing CloudWatch custom metrics use the **same starting field**
— the telemetry event's own `timestamp` (set by the publisher/simulator
at generation time, before any AWS transport), never an SQS/Lambda/
CloudWatch ingestion timestamp:

| Metric | Formula (as implemented) | Emitted by | Emitted when |
|---|---|---|---|
| `DetectionLatency` | `now() - telemetry.timestamp`, computed **immediately after** `detect_telemetry()` returns, **before** the Analyzer's own DynamoDB writes, metric publish, or remediation-trigger submission | `anomaly_detector/handler.py`, `_publish_detection_metrics` | Only when `detected=True` and telemetry timestamp parses and is not in the future |
| `RecoveryLatency` | `now() - detection_result.timestamp` (the SAME originating telemetry timestamp, carried unchanged through the whole Step Functions workflow input), computed at Executor completion | `remediation/executor.py` | Only on `ExecutionStatus.SUCCESS` |

This was confirmed by direct source inspection (`shared/metrics.py`,
`anomaly_detector/handler.py`, `remediation/executor.py`) — not assumed.
`infrastructure/README.md`'s pre-existing "Latency calculation" section
already documented this correctly; Step 36 re-verified it against the
current code rather than trusting the documentation blindly.

**Important, newly-confirmed nuance**: because `DetectionLatency`'s `now()`
is captured *before* `MetricsClient`'s own lazy boto3 CloudWatch client is
constructed (that construction happens inside the very `put_milliseconds`
call that follows), `DetectionLatency` does **not** include the Analyzer's
own metric-publish call, its DynamoDB writes, or its remediation-trigger
submission. It is a tight measurement of "telemetry generation time" to
"the moment the Analyzer finishes classifying the telemetry" — which, in
practice, given `detect_telemetry()` is a microsecond-scale deterministic
function, is very close to "telemetry generation time to Analyzer
invocation start." `RecoveryLatency` therefore = `DetectionLatency`'s
underlying pipeline latency **plus** detection processing **plus**
detection-to-remediation-start latency **plus** the full remediation
workflow duration, combined into one number — Step 28's own README
already flagged this as "not MTTR"; Step 36 confirms it precisely.

### 3.2 The Step 36 phase model

Introduced in `tools/performance_analysis.py` (`PhaseLatencies`) —
computed entirely from **already-existing AWS-native evidence**, not new
application instrumentation (see §3.3):

| Phase | Definition | Start | End |
|---|---|---|---|
| `telemetry_to_detection_ms` | End-to-end ingestion latency | telemetry `timestamp` | Analyzer Lambda's CloudWatch Logs `REPORT` line wall-clock time (full Analyzer invocation completion) |
| `detection_processing_ms` | Analyzer's own pure compute time | Analyzer invocation start | Analyzer invocation end (the `REPORT` line's own `Duration` field) |
| `detection_to_remediation_start_ms` | Async handoff + StartExecution latency | Analyzer `REPORT` line time | Step Functions execution `startDate` |
| `remediation_workflow_ms` | Pure Step Functions execution duration | execution `startDate` | execution `stopDate` |
| `telemetry_to_recovery_ms` | Full pipeline, telemetry to workflow completion | telemetry `timestamp` | execution `stopDate` |
| `planner_duration_ms` / `executor_duration_ms` / `verifier_duration_ms` | Per-state wall-clock duration (includes that Lambda's own cold start, if any) | that state's `TaskStateEntered` | that state's `TaskStateExited` |

**`telemetry_to_detection_ms` here is deliberately NOT identical to the
production `DetectionLatency` metric** — it measures to the Analyzer's
full invocation completion (the only precise, independently-obtainable
timestamp available from CloudWatch Logs), not to the exact mid-invocation
moment `DetectionLatency` is calculated. It is therefore a **slightly
conservative (higher) proxy** for `DetectionLatency`, not a substitute
claiming bit-for-bit equivalence. See §6.3 for the measured size of this
gap and independent cross-verification against the real metric.

### 3.3 Why no new CloudWatch metric, Lambda code change, or ASL change was needed

Every phase boundary above is obtainable from evidence AWS already
produces automatically, with zero changes to business logic:

- The telemetry timestamp: already present in every telemetry payload.
- Analyzer/SQS-consumer invocation timing + cold-start detection: every
  Lambda invocation already emits a `REPORT ...` CloudWatch Logs line
  (`Duration`, `Billed Duration`, and — only on a cold start — `Init
  Duration`) with no code change required to produce it.
- Correlating a specific invocation to a specific test event: the
  Analyzer already logs the full `DetectionResult` (including `node_id`)
  at INFO via `_log_result`; the SQS consumer already logs `node_id` in
  its own "processed successfully" line. Both pre-date Step 36.
- Step Functions execution start/stop and every state's entered/exited
  timestamp: `stepfunctions:DescribeExecution`/`GetExecutionHistory`
  already return this for every execution, with no ASL change needed.
- The exact execution name for a given test event: already fully
  deterministic and reproducible from `lambda_functions/remediation_trigger/trigger.py:compute_execution_name(node_id, timestamp, fault_type)`
  — no new lookup mechanism was needed.

**Decision**: Step 36 adds no new CloudWatch metric (no
`TelemetryToDetectionLatency`, `DetectionProcessingLatency`,
`DetectionToRemediationLatency`, `RemediationWorkflowLatency`, or
`TelemetryToRecoveryLatency` metric was created), makes no change to any
deployed Lambda's business logic, and makes no change to
`step_functions/remediation_workflow.json`. The entire phase model in
§3.2 is produced by a **local, non-deployed analysis tool**
(`tools/performance_analysis.py`) run against AWS-native evidence after
the fact. This directly satisfies the instruction to prefer lightweight
instrumentation and existing AWS metadata over new code.

## 4. Test setup

- **Path used**: the real, unmodified `IoT Core -> sentinelgrid/telemetry
  -> SentinelGridTelemetryQueue (SQS) -> SentinelGridSqsTelemetryConsumer
  -> SentinelGridAnomalyDetector -> SentinelGridRemediationTrigger ->
  SentinelGridRemediationWorkflow (Step Functions) -> Planner -> Executor
  -> Verifier` pipeline — no direct Lambda invocation, no manual
  DynamoDB writes, no manual Step Functions start, no manually-published
  CloudWatch metric.
- **Fault profile**: `subsystem=power`, `load=97.8` (inside the
  `(95.0, 100.0)` overload band — `detector.py`'s `NUMERIC_FIELD_SPECS`),
  `voltage=230.0`, `frequency=50.0` — the exact MEDIUM `NODE_OVERLOAD`
  profile already proven (Steps 29/33/34) to trigger automatic
  remediation.
- **Sample size**: 10 events — a small, statistically-limited but
  explicitly-authorized starting sample (not a load test), each with a
  unique `node_id` (`power-step36-perf-<index>-<run_id>`) and its own
  generation timestamp.
- **Spacing**: 20 seconds between publishes, to keep evidence
  unambiguous per event and avoid queue/batch contention obscuring
  baseline latency.
- **Tooling**: `scripts/step36_live_performance_test.py` (publish-only —
  makes exactly one AWS call per event, `iot-data:Publish`) and
  `scripts/step36_analyze_results.py` (read-only — `logs:FilterLogEvents`,
  `logs:GetLogEvents`, `stepfunctions:DescribeExecution`,
  `stepfunctions:GetExecutionHistory`).

## 5. Test procedure

1. Captured a pre-test baseline: SQS queue/DLQ depths (0/0), and exact
   `Select COUNT` scan counts on all four DynamoDB tables.
2. Ran `scripts/step36_live_performance_test.py --count 10
   --spacing-seconds 20` (run_id `20260914191207`), publishing 10 unique
   controlled events over ~189 seconds.
3. Waited for the 10th event's deterministically-predicted Step Functions
   execution to reach a terminal status (confirmed `SUCCEEDED`).
4. Ran `scripts/step36_analyze_results.py`, which independently derived
   every phase latency in §3.2 for all 10 events from CloudWatch Logs and
   Step Functions execution history.
5. Cross-checked the derived `telemetry_to_detection_ms`/
   `telemetry_to_recovery_ms` values against the real, production
   `DetectionLatency`/`RecoveryLatency` CloudWatch metric datapoints for
   the same time window (see §6.3).
6. Verified no side effects: DLQ still 0/0, no duplicate telemetry
   records per test `node_id` (each exactly 1), and all four table counts
   grew by exactly 10 each, matching 10 successful, non-duplicated
   detections and recoveries.

Raw published-event record: `docs/step36_live_test_events.json`.
Raw per-event analysis + summary: `docs/step36_live_test_results.json`.

## 6. Results

### 6.1 Detection latency (`telemetry_to_detection_ms`, independently derived, n=10)

| Min | Median | Mean | P90 | P95 | Max | Threshold | % Meeting Threshold |
|---|---|---|---|---|---|---|---|
| 656.3 ms | 710.9 ms | 1659.3 ms | 1732.2 ms | 5932.9 ms | 10133.6 ms | 10,000 ms | **9/10 = 90%** |

### 6.2 Recovery latency (`telemetry_to_recovery_ms`, independently derived, n=10)

| Min | Median | Mean | P90 | P95 | Max | Threshold | % Meeting Threshold |
|---|---|---|---|---|---|---|---|
| 1554.3 ms | 1611.0 ms | 3535.6 ms | 3568.3 ms | 12252.9 ms | 20937.6 ms | 30,000 ms | **10/10 = 100%** |

### 6.3 Remediation workflow duration (`remediation_workflow_ms`, Step Functions native `startDate`→`stopDate`, n=10)

| Min | Median | Mean | P90 | P95 | Max |
|---|---|---|---|---|---|
| 569 ms | 659 ms | 1440 ms | 1488.7 ms | 5015.3 ms | 8542 ms |

### 6.4 Other phases (n=10)

| Phase | Min | Median | Mean | P90 | P95 | Max |
|---|---|---|---|---|---|---|
| `detection_processing_ms` (Analyzer's own `Duration`) | 509.4 ms | 537.6 ms | 1187.8 ms | 1206.7 ms | 4146.3 ms | 7085.8 ms |
| `detection_to_remediation_start_ms` | 215 ms | 236.5 ms | 436.3 ms | 454.8 ms | 1358.4 ms | 2262 ms |

### 6.5 Warm-subset diagnostic (n=9, excludes the one cold-start event — see §7)

This is **diagnostic evidence for the bottleneck investigation, not a
substitute compliance sample** (see §9 Limitations for why 9 samples
cannot support an NFR claim either):

| Metric | Min | Median | Mean | P90 | P95 | Max |
|---|---|---|---|---|---|---|
| Detection latency | 656.3 ms | 706.8 ms | 717.7 ms | 767.4 ms | 783.1 ms | 798.7 ms |
| Recovery latency | 1554.3 ms | 1602.3 ms | 1602.1 ms | 1635.7 ms | 1637.0 ms | 1638.3 ms |
| Workflow duration | 569 ms | 659 ms | 650.9 ms | 689 ms | 697 ms | 705 ms |

### 6.6 Independent cross-check against the real `DetectionLatency`/`RecoveryLatency` metrics

CloudWatch's custom metrics here use **standard (60-second) resolution**
(no `StorageResolution=1` is set anywhere in `shared/metrics.py`), so
`GetMetricStatistics` cannot return sub-minute datapoints regardless of
the requested `Period` — confirmed empirically: a `Period=1` request
still returned one datapoint per 60-second bucket. This is an honest
resolution limitation, not a bug, and is exactly why this step's
per-event analysis is built from CloudWatch Logs + Step Functions
evidence rather than the metric's own datapoints.

Per-minute `Average` datapoints for the test window (`Subsystem=power,
FaultType=NODE_OVERLOAD`):

| Minute | DetectionLatency Avg | RecoveryLatency Avg | Events in this minute |
|---|---|---|---|
| 00:42 IST | 1987.97 ms | 7251.68 ms | event 0 only (the cold-start event) |
| 00:43 IST | 397.65 ms | 1401.63 ms | events 1-2 |
| 00:44 IST | 363.16 ms | 1367.50 ms | events 3-5 |
| 00:45 IST | 438.85 ms | 1416.57 ms | events 6-9 |

The real metric's warm-event values (~360-440 ms) are **lower** than this
step's own `telemetry_to_detection_ms` reproduction (~660-800 ms per
event) — consistent with, and fully explained by, §3.2's finding that
`DetectionLatency`'s own `now()` is captured mid-invocation (right after
classification), before the Analyzer's remaining persistence/metric-
publish/trigger-submission work, while this step's proxy uses the full
invocation's `REPORT` line (end of invocation). **The two measurements
agree on order of magnitude and both show the same cold-start minute is
the outlier** — this is the independent reproduction Step 36 set out to
establish (§1, question 3).

## 7. Bottleneck analysis (Phase G)

**Finding: the elevated latency is concentrated almost entirely in the
FIRST event of the test run — a simultaneous cold start of every Lambda
in the chain — not in steady-state detection or remediation logic.**

Evidence, event-by-event (`docs/step36_live_test_results.json`):

| | Event 0 (first, cold) | Events 1-9 (warm) |
|---|---|---|
| Analyzer cold start (`Init Duration` present) | **Yes** | No (0/9) |
| SQS consumer cold start | **Yes** | No (0/9) |
| Analyzer `Duration` (own compute+AWS-call time) | 7085.8 ms | 509-554 ms |
| `telemetry_to_detection_ms` | 10133.6 ms | 656-799 ms |
| `remediation_workflow_ms` | 8542 ms | 569-705 ms |
| Planner/Executor/Verifier state durations | 2582/5454/459 ms | 104-149 / 274-437 / 76-119 ms |

**Root cause, as far as this evidence shows**:

1. Event 0 is the pipeline's first invocation after being idle — both the
   SQS consumer AND the Analyzer needed a genuine Lambda cold start
   (execution environment initialization). This is consistent with AWS's
   documented SQS-event-source-mapping poller ramp-up behavior after
   queue idleness, though this step did not (and could not, without a
   privileged AWS internal view) directly measure the poller's own
   scaling delay in isolation — the evidence available (cold `Init
   Duration` on both Lambdas) supports a Lambda-cold-start explanation as
   at least a major contributor, alongside possible SQS poller ramp-up.
2. This project's own **lazy boto3 initialization** design (documented,
   deliberate, in every AWS-facing adapter — `event_store.py`,
   `node_state_store.py`, `shared/metrics.py`, `remediation_trigger_client.py`
   — "constructing this class never touches AWS by itself") means a
   fresh execution environment's FIRST invocation pays for every boto3
   client construction (DynamoDB, CloudWatch, Lambda invoke) **inside**
   that invocation's own `Duration`, not inside the separately-reported
   `Init Duration`. This directly explains why the Analyzer's `Duration`
   itself (509 ms warm vs. 7086 ms cold) — not just its `Init Duration` —
   was ~13x higher on the cold event: on a cold start, this one
   invocation pays for TLS/handshake setup to DynamoDB (twice — anomaly
   event write, node-state update), CloudWatch (metric publish), and
   Lambda invoke (trigger submission) all at once; on a warm invocation,
   every one of those clients/connections is already established and
   reused.
3. The SAME effect compounds through Planner, Executor, and Verifier: this
   was ALSO their first invocation of the whole test run, so all three
   ALSO cold-started simultaneously (evidenced by their own state
   durations being 5-20x higher for event 0 than for every subsequent
   event), which is why `remediation_workflow_ms` for event 0 (8542 ms)
   is dramatically higher than the 9 warm executions (569-705 ms,
   matching the "previous controlled Step Functions executions ~1.5-1.6s"
   evidence closely once its own upstream latency is excluded).

**This directly and specifically explains the Step 29 ~14.97s
`DetectionLatency` / ~23.9s `RecoveryLatency` observation**: Step 29's
test methodology was a single, isolated, controlled end-to-end event —
by construction, the first (and only) invocation of every Lambda in the
chain, i.e. exactly the cold-start scenario reproduced here (event 0:
10.1s detection / 20.9s recovery — the same order of magnitude, same
phenomenon, plausibly-different exact values given different account/day
conditions). **This step's evidence refines, rather than contradicts,
Step 29's finding**: the ~15s figure was not a property of the detection
or remediation LOGIC, but of a cold, previously-idle pipeline.

**No optimization was applied.** Per Step 36's explicit instructions, if
a clearly safe, minimal, and necessary configuration change were found,
it would be documented and proposed for approval, not applied
automatically. **No such change was identified as necessary to establish
this baseline** — Step 36's purpose was measurement, and the measurement
is now established: this is a cold-start/idle-pipeline effect. Any future
mitigation (e.g., provisioned concurrency, a keep-warm schedule, or
narrowing the lazy-boto3 client construction to Lambda INIT phase) is
explicitly left for a future step's decision, not proposed or applied
here.

## 8. NFR assessment

**NFR-1 (detection ≤10s for ≥95% of cases): NOT DEMONSTRATED, and the
one sample available shows a threshold violation.**

- Full sample (n=10, includes the cold-start event): 9/10 = 90% within
  10 seconds — below the 95% target, and n=10 is far too small to make
  any statistically confident claim about a 95%-of-population
  requirement in either direction.
- The single miss (event 0, 10.13s) is explained (§7) as a cold-start
  artifact of this test's own methodology (first event after idle) —
  this is a legitimate finding, not a fabricated excuse: every warm event
  (9/9) was well under 1 second. But **excluding it to claim "100%" would
  misrepresent real-world behavior**, since a production system that has
  been idle (a genuinely realistic operating condition, not a test
  artifact alone) will exhibit the same cold-start behavior on its next
  event.
- **Honest statement**: detection is fast and well within budget once the
  pipeline is warm (median 707-711 ms either way), but the FIRST
  detection after any idle period can exceed the 10-second budget. A
  properly powered statistical claim requires a much larger sample
  (dozens to hundreds of events, deliberately including both warm and
  cold-start conditions in realistic proportion) — not attempted in Step
  36, which was scoped to a 10-event baseline.

**NFR-2 (recovery ≤30s for ≥90% of cases): NOT DEMONSTRATED, but the
available sample shows no violation.**

- Full sample (n=10): 10/10 = 100% within 30 seconds, including the
  cold-start event (20.9s, still under budget).
- Median recovery time (warm pipeline) is ~1.6 seconds — a wide margin
  under the 30-second budget.
- **Honest statement**: no NFR-2 violation was observed in this sample,
  but n=10 remains too small to assert ≥90% compliance across the full
  population with statistical confidence. The result is consistent with,
  but does not prove, NFR-2 compliance.

**NFR-3 (500 virtual nodes): NOT EVALUATED in Step 36.** This step used a
10-event sample specifically to establish measurement correctness first,
per its own explicit scope — no claim about 500-node scale is made.

## 9. Limitations

- **Sample size (n=10)** is a deliberately small, explicitly-authorized
  starting sample — not sufficient to statistically validate a ≥95%/≥90%
  population-level requirement in either direction.
- **`telemetry_to_detection_ms` here is not bit-for-bit identical to the
  production `DetectionLatency` metric** (§3.2, §6.6) — it is a
  conservative (higher) proxy using the Analyzer's full invocation
  completion, not the exact mid-invocation calculation point. The two
  were cross-checked and agree in order of magnitude and cold-start
  identification, but are not the same number.
- **CloudWatch custom metrics have 60-second resolution** here (no
  `StorageResolution=1`), so the metric's own datapoints cannot be used
  for sub-minute per-event correlation — this is exactly why the
  CloudWatch-Logs/Step-Functions-based approach was used instead.
- **Only one cold-start scenario was captured** (the test's own first
  event) — this step cannot characterize the DISTRIBUTION of cold-start
  frequency/severity under realistic, intermittent traffic patterns.
- **This step did not test concurrent/simultaneous events** (by design —
  events were deliberately spaced 20s apart to keep evidence
  unambiguous) — queue contention or concurrency effects under load are
  unmeasured.
- **NFR-3 (500 nodes) was not evaluated.**
- **No optimization was attempted or applied** — this is a measurement
  baseline only, as scoped.

## 10. Recommended next step

Based on this evidence, a future step should consider:

1. A larger, statistically powered live sample (tens to low hundreds of
   events) under realistic mixed warm/cold conditions, to make a
   defensible NFR-1/NFR-2 population-level claim.
2. Explicitly measuring cold-start frequency/impact under realistic,
   intermittent (not densely-spaced) traffic, since that is closer to
   how idle periods would occur in production.
3. If cold-start latency is judged unacceptable for NFR-1 after a larger
   sample, evaluate (as a SEPARATE, deliberately-approved step — not
   automatically) a targeted, minimal mitigation such as Lambda
   provisioned concurrency on the Analyzer/SQS-consumer/remediation
   Lambdas, or a lightweight scheduled keep-warm invocation — weighed
   against this project's explicit low-cost/student-budget constraint.
4. A dedicated NFR-3 scale test (approaching 500 virtual nodes) as its
   own controlled, explicitly-scoped step.

None of the above is implemented by Step 36.

## Addendum (Step 37 — recommendation #4 above, now performed)

Step 37 performed the dedicated 500-node NFR-3 scale test recommended
above. It did not repeat or revise this document's own detection/
recovery latency measurements or conclusions — those stand exactly as
recorded above. It did surface one new, directly relevant finding: this
AWS account's Lambda concurrent-execution limit is 10 (not AWS's typical
1,000-execution default), which caused heavy `Throttles` (not `Errors`)
on both `SentinelGridSqsTelemetryConsumer` and `SentinelGridAnomalyDetector`
under a 500-node/100-msg/s load — absorbed by Lambda's automatic retries
with zero data loss, but adding significant processing latency (a
~11.5-minute drain for a load published in under a minute). This
constraint did not affect, and is not reflected in, the n=10 measurements
above (that test's load was far below the concurrency ceiling). Full
detail: `docs/scale-validation.md`.
