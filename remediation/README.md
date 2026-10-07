# Remediation (local, simulated)

## Purpose

This package answers one question deterministically: **given a
`DetectionResult`, what corrective action (if any) should be taken, and is
it safe to take automatically?**

It is Step 12 of SentinelGrid's incremental build: the local prototype for
the "Plan" and "Execute" stages of the project's eventual MAPE-K loop (see
the root README). Like the anomaly detector before it
(`lambda_functions/anomaly_detector/`), this is built and thoroughly tested
**locally first, with no AWS dependency at all**, before any cloud
integration is attempted.

This step is a **safe simulation**. Nothing in this package changes real
node state, network configuration, sensor calibration, or any other
external system — it only decides and records what *would* be done.

## Architecture

```
raw telemetry
      |
      v
detect_telemetry()                 (lambda_functions/anomaly_detector/detector.py)
      |
      v
DetectionResult
      |
      v
RemediationEngine.plan()          (engine.py)
      |
      v
RemediationPlan                   (models.py)
      |
      v  (only when remediation_plan.remediation_required)
RemediationExecutor.execute()     (engine.py)
      |
      v
RemediationResult (or None)       (models.py)
```

`integration.py`'s `process_telemetry()` wires the whole left column
together in one call — see [Local integration layer](#local-integration-layer)
below. Steps 12/13 kept this deliberately in two files: `engine.py` never
imports `detect_telemetry`, and `detector.py` has never been (and is not)
modified by this package — detection and remediation remain independently
testable and independently correct.

- **`models.py`** — plain dataclasses (`RemediationAction`, `RemediationPlan`,
  `RemediationResult`) and two `str`-backed enums (`ActionType`,
  `ExecutionStatus`), mirroring the style of
  `lambda_functions/anomaly_detector/detector.py`. Every model has a
  `to_dict()` method and is trivially JSON-serializable.
- **`engine.py`** — `RemediationEngine.plan()` maps a `DetectionResult` to a
  `RemediationPlan` using a fixed, explicit table (see below).
  `RemediationExecutor.execute()` turns a `RemediationPlan` into a
  `RemediationResult`, simulating the outcome without touching anything
  real.

Both classes are pure functions of their input: no randomness, no wall
clock, no file/network/AWS I/O. `RemediationPlan.timestamp` is carried
through from `DetectionResult.timestamp` (the telemetry event's own
timestamp) rather than the wall-clock planning time, specifically so
planning and execution stay deterministic and reproducible in tests.

The only import outside the standard library is
`lambda_functions.anomaly_detector.detector` (for `DetectionResult`,
`FaultType`, `Severity`) — the detector module itself has no AWS or network
dependency, so this package carries none either. It does **not** import
`handler.py` or `event_store.py`.

## Fault -> remediation mapping

| `fault_type`        | Action               | Automatic?                              |
|----------------------|----------------------|------------------------------------------|
| `SENSOR_DROPOUT`      | `RESTART_SENSOR`      | Yes                                       |
| `NETWORK_PARTITION`   | `RECONNECT_NODE`      | Yes                                       |
| `DATA_CORRUPTION`     | `RESET_SENSOR_STATE`  | Yes (regardless of severity)              |
| `NODE_OVERLOAD`       | `REDUCE_LOAD`         | Only at `MEDIUM` severity                 |
| `NODE_OVERLOAD`       | `REDUCE_LOAD`         | **No** at `HIGH`/`CRITICAL` — escalate    |
| `INVALID_INPUT`       | `REJECT_INPUT`        | No — always escalate, never repair        |
| anything else         | `NO_ACTION`            | No — unsupported, nothing is planned      |

Only `NODE_OVERLOAD` is severity-gated. The current detector only ever
produces `NODE_OVERLOAD` at `MEDIUM` severity (see
`detector.py::_check_numeric_field`), so the `HIGH`/`CRITICAL` branch is a
forward-looking safety policy, not something exercised by real detector
output today — it is still fully implemented and tested (see
`tests/test_remediation.py`) so the policy is correct the day the detector
(or a future one) does produce it.

No action beyond this table exists. The engine never invents a fake
actuator command (e.g. there is no `shut_down_power_grid` or
`open_water_valve`) — `ActionType` is a closed enum of exactly the six
values above, including `NO_ACTION`.

## Safety policy

- **Unknown fault type** -> `NO_ACTION`, `remediation_required=False`. The
  engine never guesses.
- **`INVALID_INPUT`** -> always `REJECT_INPUT`, always `automatic=False`,
  always `escalation_required=True`. The engine never attempts to repair
  data it doesn't trust. Because the detector can produce `INVALID_INPUT`
  with `node_id`/`subsystem` missing (that's often exactly what triggered
  the classification), this check runs *before* the missing-field
  completeness check below — rejecting untrusted input doesn't require
  knowing which node it came from.
- **Missing required fields** (`node_id`/`subsystem` for anything other
  than `INVALID_INPUT`, or a missing/empty `fault_type`, or `detection is
  None`) -> safe failure: `NO_ACTION`, `remediation_required=False`. The
  engine never guesses a target node.
- **`detected == False`** -> `NO_ACTION`, `remediation_required=False`.
  Nothing to remediate.
- **Severity gates `NODE_OVERLOAD` only**: `MEDIUM` is automatic; `HIGH`
  and `CRITICAL` require escalation rather than a more aggressive invented
  action.
- Deterministic, no randomness, no external calls, no AWS dependency.

## Planning vs. execution

- **Planning** (`RemediationEngine.plan()`) is a pure decision: it never
  performs any action, only decides what *should* happen and whether it is
  safe to do so automatically.
- **Execution** (`RemediationExecutor.execute()`) is a **simulation** of
  carrying that decision out. For a supported, automatic plan, it reports
  `RemediationResult(success=True, status=SUCCESS, simulated=True, ...)` —
  simulated success, not a real state change. For a plan requiring
  escalation (`automatic=False`), it reports `status=ESCALATED,
  success=False` — it never pretends an escalated or rejected action was
  carried out. `RemediationResult.simulated` is always `True` in this step,
  by design, so a future real executor can reuse this exact model shape and
  set it to `False` without changing anything else.

## Local integration layer

Step 13 added `integration.py`, which wires `detect_telemetry()` directly
into `RemediationEngine`/`RemediationExecutor` so the whole pipeline can be
exercised from raw telemetry in one call:

```python
from remediation import process_telemetry

result = process_telemetry(telemetry)  # a RemediationIntegrationResult
```

`process_telemetry()`:

1. Calls `detect_telemetry(telemetry)` to get a `DetectionResult`. It never
   modifies this result — the detector remains solely responsible for
   validation, classification, and severity.
2. Calls `RemediationEngine().plan(detection_result)` to get a
   `RemediationPlan`.
3. Calls `RemediationExecutor().execute(plan)` **only if**
   `plan.remediation_required` is `True`. For a normal (non-anomalous)
   reading, `remediation_result` is `None` — no execution is attempted,
   rather than executing a no-op and calling that `None`.
4. Returns a `RemediationIntegrationResult(detection_result,
   remediation_plan, remediation_result)` — serializable via `to_dict()`,
   like every other model in this package.

This module does **not** call
`lambda_functions.anomaly_detector.handler` (the AWS Lambda adapter) or
`event_store` (DynamoDB persistence) — it has no AWS dependency, no
network I/O, and no persistent state, exactly like `engine.py`. A fresh
`RemediationEngine`/`RemediationExecutor` is constructed on each call;
neither class holds instance state, so this introduces no shared mutable
state across calls.

## Example flow

```python
from lambda_functions.anomaly_detector.detector import DetectionResult
from remediation import RemediationEngine, RemediationExecutor

detection = DetectionResult(
    detected=True,
    fault_type="NODE_OVERLOAD",
    severity="MEDIUM",
    node_id="power-007",
    subsystem="power",
    timestamp="2026-01-01T00:05:00+00:00",
    reason="Field 'load' value 97.8 is within the overload band (95.0, 100.0).",
    evidence={"field": "load", "value": 97.8, "overload_range": (95.0, 100.0)},
)

plan = RemediationEngine().plan(detection)
# RemediationPlan(remediation_required=True, action=RemediationAction(
#     action_type=ActionType.REDUCE_LOAD, ...), automatic=True, ...)

result = RemediationExecutor().execute(plan)
# RemediationResult(success=True, status=ExecutionStatus.SUCCESS,
#     action=ActionType.REDUCE_LOAD, simulated=True, ...)
```

A `HIGH`-severity `NODE_OVERLOAD` instead produces `automatic=False,
escalation_required=True`, and executing that plan yields
`RemediationResult(success=False, status=ExecutionStatus.ESCALATED, ...)`.

The same flow via the integration layer, starting from raw telemetry
instead of a hand-built `DetectionResult`:

```python
from remediation import process_telemetry

telemetry = {
    "node_id": "power-007",
    "subsystem": "power",
    "timestamp": "2026-01-01T00:05:00+00:00",
    "status": "ONLINE",
    "data": {"load": 97.8, "voltage": 230.0, "frequency": 50.0},
}

result = process_telemetry(telemetry)
# result.detection_result.fault_type   == "NODE_OVERLOAD"
# result.remediation_plan.action.action_type == ActionType.REDUCE_LOAD
# result.remediation_result.status     == ExecutionStatus.SUCCESS

result.to_dict()  # {"detection_result": {...}, "remediation_plan": {...}, "remediation_result": {...}}
```

For normal telemetry (`detection_result.detected is False`),
`result.remediation_plan.remediation_required` is `False` and
`result.remediation_result` is `None`.

## Limitations and future AWS integration

- Detection and remediation are now wired together **locally**
  (`integration.py`), but this is still not connected to AWS in any way:
  the Lambda handler (`lambda_functions/anomaly_detector/handler.py`)
  does not call into this package, and nothing here calls the Lambda
  handler or `event_store.py`.
- No persistent state or idempotency tracking yet — planning the same
  `DetectionResult` twice is safe (it's a pure function and always
  produces an equal `RemediationPlan`), but nothing here deduplicates
  *executions* across repeated Lambda invocations. That distributed
  idempotency (the DynamoDB conditional-write pattern used by
  `lambda_functions/anomaly_detector/event_store.py`) will be added when
  this layer is wired into AWS.
- No real actuation exists or is planned as fake — this project is
  software-only; real infrastructure is never controlled.
- The eventual AWS integration point is expected to be a Lambda (or Step
  Functions state) that calls `RemediationEngine.plan()` on a persisted
  anomaly event, then a separate executor step that performs (or, for
  now, continues to simulate) `RemediationExecutor.execute()`'s decision —
  mirroring how `handler.py` currently calls `detect_telemetry()`. None of
  that AWS wiring exists yet; this package remains pure, boto3-free, and
  independently testable until that step is explicitly requested.
