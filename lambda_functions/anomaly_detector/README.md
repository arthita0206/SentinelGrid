# Anomaly / Fault Detector

A local, deterministic, rule-based detector that classifies one
SentinelGrid telemetry reading as normal or anomalous. This is the
prototype for logic that will eventually run inside an AWS Lambda
function — **no Lambda function is deployed or implemented by this
step**, and the detector itself makes no AWS calls of any kind.

```python
from lambda_functions.anomaly_detector import detect_telemetry

result = detect_telemetry(telemetry.to_dict())
# result.detected, result.fault_type, result.severity, result.reason, result.evidence
```

## Why deterministic, not ML

Every classification is a plain type check or numeric range comparison
against the payload — no model, no training data, no external ML
dependency (`scikit-learn`, `numpy`, `pandas`, etc. are deliberately not
used; only the Python standard library, primarily `math.isfinite`). This
means every decision this detector makes can be traced back to one
specific rule and one specific field value — see `evidence` on every
result — which matters for an academic project where explaining *why*
something was flagged is as important as flagging it.

## Interface

```python
detect_telemetry(payload: dict) -> DetectionResult
```

`payload` is an already-parsed dict in the same shape
`shared.telemetry.Telemetry.to_dict()` produces — this function does not
parse JSON itself. **It never raises.** Malformed input produces a
`DetectionResult` with `fault_type="INVALID_INPUT"` rather than an
exception, so one bad message can never crash whatever loop or handler
calls this.

### `DetectionResult`

| Field | Type | Meaning |
|---|---|---|
| `detected` | `bool` | `False` only when telemetry is normal. |
| `fault_type` | `str \| None` | `None` when normal; otherwise one of the classifications below. |
| `severity` | `str` | One of `NORMAL`, `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`. |
| `node_id`, `subsystem`, `timestamp` | `str \| None` | Echoed from the payload where available (may be `None` if the payload itself was too malformed to carry them). |
| `reason` | `str` | Human-readable explanation of the specific rule that fired. |
| `evidence` | `dict` | The specific field/value(s) that triggered the result — never just "something is wrong." |

## Fault classifications

| `fault_type` | Meaning |
|---|---|
| `NONE`* | Internal-only sentinel; the field itself is `None` (not the string `"NONE"`) when nothing is detected. |
| `SENSOR_DROPOUT` | The node isn't reporting readings — see below. |
| `NETWORK_PARTITION` | Reserved — see [Network partition](#network-partition-a-known-limitation) below. Not producible by today's telemetry schema. |
| `DATA_CORRUPTION` | A reading is present but impossible, malformed, or invalid. |
| `NODE_OVERLOAD` | A reading is elevated into the simulator's own defined overload band. |
| `INVALID_INPUT` | The message itself can't be processed (missing/invalid `node_id`, `subsystem`, or `timestamp`). Not one of the four SRS fault types — this is a message-format problem, not a node fault. |

\* `FaultType.NONE` exists in the enum for completeness ("at minimum
support NONE, SENSOR_DROPOUT, ..."); it is never the literal value of
`DetectionResult.fault_type`, which uses `None`/JSON `null` instead.

## Severity levels

| Severity | When |
|---|---|
| `NORMAL` | No anomaly. |
| `LOW` | An invalid categorical value (e.g. `signal_state` not in `RED/YELLOW/GREEN`) — a data-quality glitch, not a physically dangerous reading. |
| `MEDIUM` | `NODE_OVERLOAD` — an elevated-but-plausible operating condition. |
| `HIGH` | `SENSOR_DROPOUT`, `NETWORK_PARTITION` (loss of visibility), or `DATA_CORRUPTION` where the value is a real, finite number but physically impossible for that field. |
| `CRITICAL` | `INVALID_INPUT`, or `DATA_CORRUPTION` where the value is the wrong type entirely (non-numeric, `NaN`, `±Infinity`) — a completely unusable reading. |

## Where the thresholds come from

Every numeric boundary is read directly off the existing code, not
invented independently — see `NUMERIC_FIELD_SPECS` in
[detector.py](detector.py):

| Subsystem.field | Normal range | Overload band | Source |
|---|---|---|---|
| power.load | 0–100 | **95–100** | `simulator/subsystems/power.py` clamp; `fault_injection/overload.py` |
| power.voltage | 200–250 | — | `simulator/subsystems/power.py` clamp |
| power.frequency | 49–51 | — | `simulator/subsystems/power.py` clamp |
| water.pressure | 0–7 | **7–9** | `simulator/subsystems/water.py` clamp (1–8); `fault_injection/overload.py` (7–9) |
| water.flow_rate | 0–120 | **120–250** | `simulator/subsystems/water.py` clamp; `fault_injection/overload.py` |
| water.reservoir_level | 0–100 | — | `simulator/subsystems/water.py` clamp |
| traffic.vehicle_count | 0–200 | **200–400** | `simulator/subsystems/traffic.py` clamp; `fault_injection/overload.py` |
| traffic.average_speed | 0–120 | — | `simulator/subsystems/traffic.py` clamp |

A value outside the normal range (and outside the overload band, where
one exists) is `DATA_CORRUPTION`, corroborated by
`fault_injection/corruption.py`'s injected ranges, all of which land well
outside these boundaries (e.g. corrupted voltage is 500–800, far past the
200–250 valid ceiling).

**Known, documented overlap**: for `power.load` and `water.pressure`, the
overload band partially or fully overlaps the simulator's own normal
operating range (e.g. the node can organically drift as high as 100%
load, and the overload fault also targets 95–100%). This detector checks
the overload band *first*, so a rare organic high reading in that band
will be classified `NODE_OVERLOAD` even with no fault injected. This is
an accepted characteristic of any static-threshold detector — the
alternative (never flagging that band) would miss real overload faults
entirely — and is called out here rather than hidden.

**Design choice, not just replication**: for fields with an overload
band, this detector treats anything *above* the overload band's own
ceiling as `DATA_CORRUPTION` (e.g. water flow_rate > 250, traffic
vehicle_count > 400) — a generous backstop for values beyond anything the
system ever intentionally produces, not just an exact copy of
`fault_injection/corruption.py`'s numbers.

**One anomaly, one reason**: fields are checked in a fixed order
(`load, voltage, frequency` / `pressure, flow_rate, reservoir_level` /
`vehicle_count, average_speed, signal_state`), and the first anomalous
field wins. If a reading has several problems at once, only one is
reported — a deliberate simplicity choice for a v1 explainable detector,
not an attempt to aggregate multiple simultaneous causes.

## Subsystem-specific notes

- **Traffic `average_speed`** never triggers `NODE_OVERLOAD` on its own.
  The traffic overload fault (`fault_injection/overload.py`) actually
  *lowers* speed toward 0 (gridlock) while raising `vehicle_count` — but
  a low speed alone is a completely normal reading (a car stopped at a
  red light), so only `vehicle_count` exceeding its normal ceiling is
  used as the overload signal for this subsystem.
- **`signal_state`** is checked against the exact set the simulator uses
  (`RED`, `YELLOW`, `GREEN`) — anything else, including the fault
  injector's `"UNKNOWN"` corruption value, is `DATA_CORRUPTION` (`LOW`
  severity, since an invalid label is a data-quality issue, not a
  physically dangerous reading).

## Sensor dropout

`SENSOR_DROPOUT` is classified when telemetry *is* received but indicates
the node isn't really reporting:

1. `status` is present and is not exactly `"ONLINE"` (today's simulator
   always sets `"ONLINE"` regardless of fault state, so this path is
   forward-compatible/defensive rather than exercised by real output).
2. `data` is missing, `null`, not an object, or empty.
3. `data` is present but missing one or more of the subsystem's required
   fields.

This is deliberately distinct from `DATA_CORRUPTION`: **missing** implies
the sensor didn't report a value at all (dropout-like); **present but
invalid** implies a value *was* reported but is wrong (corruption-like).

**Important limitation**: `SensorDropoutFault` (see
`fault_injection/faults.py`) works by suppressing publishing entirely
while active — so in the real pipeline, a dropped-out node simply produces
*no message* during the fault window. A single-message detector can never
observe that absence directly; it can only classify `SENSOR_DROPOUT` from
the *content* rules above (status/missing-data), which mostly cover a
node that reports something incomplete rather than nothing at all.
Detecting a true "no heartbeat for N seconds" dropout requires tracking
state across messages over time (e.g. in DynamoDB or a future Analyzer
Lambda) — out of scope for this stateless, single-message detector.

## Network partition: a known limitation

`NetworkPartitionFault` has the **exact same mechanical effect** on
telemetry as `SensorDropoutFault` — both simply suppress publishing (see
`fault_injection/faults.py`'s shared `blocks_publishing()` logic). Nothing
in the current `Telemetry` schema (`node_id`, `subsystem`, `timestamp`,
`status`, `data`) distinguishes *why* a node stopped publishing. Given
only the content of a message that *did* arrive, this detector cannot
reliably tell a network partition apart from a sensor dropout — and it
does not try to fake that distinction.

Instead, `detect_telemetry` includes an inert, forward-looking hook: if a
payload ever carries an optional `network_status` field set to
`"PARTITIONED"` or `"UNREACHABLE"`, it is classified `NETWORK_PARTITION`.
**No part of the current schema or simulator ever sets this field** — this
hook exists so that if a future step adds real network-state metadata
(e.g. from AWS IoT Core's connection lifecycle events), the classification
is ready to receive it without changing the detector's shape.

## What this detector can and cannot do today

**Can:**
- Classify a single, self-contained telemetry reading as normal or
  anomalous, deterministically and explainably.
- Distinguish overload (elevated-but-plausible) from corruption
  (impossible/malformed) from missing-data (dropout-like).
- Handle missing/invalid `node_id`/`subsystem`/`timestamp`, wrong types,
  `NaN`, and `±Infinity` without crashing.

**Cannot (yet):**
- Distinguish network partition from sensor dropout (see above).
- Detect a dropout by the *absence* of messages over time — it only sees
  one message at a time and has no memory between calls.
- Query DynamoDB, call AWS, or use `FaultInjectionEngine` — by design,
  it works from the payload alone.

## Lambda adapter (`handler.py`)

`detector.py` (above) and `handler.py` are deliberately separate:

| | Purpose | AWS-aware? |
|---|---|---|
| `detector.py` | All classification logic — every rule described above. | No. Plain Python, plain data in/out. |
| `handler.py` | Extracts a payload from a Lambda `event` and calls `detect_telemetry()`. Contains **zero detection rules** of its own. | Yes, but makes no AWS SDK calls — it's a pure adapter. |

```
AWS IoT event -> lambda_handler(event, context)
    -> _extract_payload(event)       # handler.py — the only AWS-facing step
    -> detect_telemetry(payload)     # detector.py — all the actual logic
    -> result.to_dict()              # returned as-is, no extra envelope
```

**No Lambda function, IoT Rule, or trigger is deployed in AWS.** This is
local, testable adapter code only — see [tests/test_anomaly_detector_handler.py](../../tests/test_anomaly_detector_handler.py).

### Input: supported event shapes

1. **The telemetry dict itself** (expected/primary shape). When an AWS IoT
   Rule's Lambda action is configured with `SELECT *`, IoT Core invokes
   the function with the selected message as the event directly — no
   envelope. This is the shape this adapter is built for.
2. **A JSON string** encoding that same dict (e.g. a Lambda console test
   event pasted as text).

No API-Gateway-style `{"body": ...}` envelope is supported — IoT Rule
Lambda actions don't produce one, and adding support for a shape this
trigger type will never actually send would just be unused complexity.
Anything else (a dict that isn't telemetry-shaped, `None`, a list, ...) is
passed straight to `detect_telemetry()`, which already turns non-dict or
malformed input into an `INVALID_INPUT` result — the handler doesn't
duplicate that logic.

### Output

`lambda_handler` returns `DetectionResult.to_dict()` directly — the exact
same shape `detect_telemetry()` already produces, with **no** Lambda
response envelope (no `statusCode`/`body`), since an IoT Rule Lambda
action doesn't need one:

```json
{
  "detected": false,
  "fault_type": null,
  "severity": "NORMAL",
  "node_id": "power-001",
  "subsystem": "power",
  "timestamp": "2026-01-01T00:00:00+00:00",
  "reason": "All telemetry fields are present and within the expected operating range.",
  "evidence": {"data": {"load": 55.2, "voltage": 230.1, "frequency": 50.01}}
}
```

Note `"fault_type": null`, not `"NONE"` — this matches `detect_telemetry`'s
existing, already-tested convention (see [Fault classifications](#fault-classifications)
above): `FaultType.NONE` is an internal enum sentinel, never the literal
value returned to a caller.

### Handler-level error handling

The only failure this adapter handles itself is an `event` that's a
string but isn't valid JSON (`json.JSONDecodeError`) — that's a transport
problem `detect_telemetry()` can't help with, since it never even
receives a Python object to inspect. That one case returns an
`INVALID_INPUT` `DetectionResult`, built the same way `detector.py`
builds one. No broad `except Exception` is used — a genuine bug elsewhere
in the call still raises normally rather than being silently swallowed.

### Operational logging

Because an IoT Rule's Lambda action invokes asynchronously, `lambda_handler`'s
return value is never visible to the MQTT publisher or anywhere else in
AWS. So before returning, the handler logs the same `DetectionResult` as
one structured line at `INFO`, via the standard-library `logging` module:

```
SentinelGrid detection result: {"detected": true, "evidence": {...}, "fault_type": "NODE_OVERLOAD", ...}
```

**This logger explicitly sets its own level to `INFO`**
(`logger.setLevel(logging.INFO)`, scoped to this one named logger — no
`basicConfig()`, no touching the root logger, no effect on any other
module). This isn't optional polish: without it, the log line is
silently dropped and never reaches CloudWatch. It was caught as a real
deployed bug — AWS Lambda's Python runtime attaches a handler to the root
logger, but leaves its level at Python's own default (`WARNING`); a named
logger with no level of its own inherits that threshold, so
`logger.info(...)` never even reaches the handler. Tests that force the
level via `caplog.at_level(...)` can pass even when this is missing (the
test harness does the job the code should be doing) — see
`test_module_logger_is_configured_for_info_level` and
`test_info_log_is_emitted_without_any_test_harness_forcing_the_level` in
[tests/test_anomaly_detector_handler.py](../../tests/test_anomaly_detector_handler.py)
for regression tests that check the logger's actual configuration
directly, independent of any test-harness override.

This is the only way to verify or diagnose a real detection operationally
(e.g. in CloudWatch Logs) today. It's purely additive observability — it
never changes what `lambda_handler` returns, and it never logs
credentials, environment variables, or the raw Lambda event; only the
`DetectionResult`'s own fields (`detected`, `fault_type`, `severity`,
`node_id`, `subsystem`, `timestamp`, `reason`, `evidence`) are logged.

### Local invocation (no AWS required)

```python
import json
from lambda_functions.anomaly_detector import lambda_handler

with open("lambda_functions/anomaly_detector/examples/normal_power_event.json") as f:
    event = json.load(f)

print(lambda_handler(event, None))
```

Two example events are provided under
[examples/](examples/): `normal_power_event.json` (no anomaly) and
`overload_power_event.json` (`NODE_OVERLOAD`).

## Deployment package and Lambda infrastructure (defined, not deployed)

CloudFormation infrastructure for running this handler as an actual AWS
Lambda function now exists under [../../infrastructure/](../../infrastructure/) —
see [infrastructure/README.md](../../infrastructure/README.md) for full
details. Summary:

- **`infrastructure/package-anomaly-detector.sh`** builds a deployment zip
  containing only `lambda_functions/` and `shared/` source files (no
  tests, docs, examples, or `venv/`) — this handler currently has zero
  third-party dependencies, so nothing is `pip install`-ed into it.
- **`infrastructure/anomaly-detector.yaml`** defines the
  `SentinelGridAnomalyDetector` Lambda function (handler:
  `lambda_functions.anomaly_detector.handler.lambda_handler`, runtime
  `python3.12` — a currently supported Lambda runtime, chosen
  independently of this project's local `.python-version` (3.9, unchanged
  for local development) since Lambda's `python3.9` runtime is
  deprecated) and a dedicated execution role with only
  `logs:CreateLogGroup`/`CreateLogStream`/`PutLogEvents` — no DynamoDB,
  IoT, or other access.
- **`infrastructure/deploy-anomaly-detector.sh`** packages, uploads to an
  S3 bucket you already control, and deploys the stack.

**Update:** as of Step 9, `SentinelGridAnomalyDetector` and its execution
role have been deployed and independently verified in AWS — confirmed
against real invocations returning `detected=false`/`NORMAL` for normal
power telemetry and `detected=true`/`NODE_OVERLOAD`/`MEDIUM` for
overloaded power telemetry. The existing `sentinelgrid_telemetry_to_dynamodb`
IoT Rule remains completely unaffected and unchanged.

## IoT -> Lambda rule (defined, not yet deployed)

[`infrastructure/anomaly-detection-rule.yaml`](../../infrastructure/anomaly-detection-rule.yaml)
defines a **new, independent** IoT Rule
(`sentinelgrid_telemetry_to_lambda`) that invokes this deployed Lambda for
every message on `sentinelgrid/telemetry`, alongside — not replacing —
the existing DynamoDB rule:

```
AWS IoT Core -> sentinelgrid/telemetry
      +--> sentinelgrid_telemetry_to_dynamodb  (existing, untouched) --> DynamoDB
      +--> sentinelgrid_telemetry_to_lambda    (new, not deployed)   --> SentinelGridAnomalyDetector
```

It grants IoT permission to invoke the function via a scoped
`AWS::Lambda::Permission` (principal `iot.amazonaws.com`, restricted to
this specific rule's ARN — never public) and adds **no new IAM role**:
an IoT Rule's Lambda action authorizes through that resource-based
permission alone, not an assumed role, so the function's existing
CloudWatch-Logs-only execution role is untouched.

**Update:** this rule has since been deployed and verified end-to-end —
real invocations return `detected=false`/`NORMAL` for normal telemetry
and `detected=true`/`NODE_OVERLOAD`/`MEDIUM` for overloaded telemetry, and
the structured detection-result log line is confirmed appearing in
CloudWatch. See [infrastructure/README.md](../../infrastructure/README.md)'s
`anomaly-detection-rule.yaml` section for the full resource list and
deployment command.

## Anomaly event persistence (`event_store.py`) — implemented, not yet deployed

[`event_store.py`](event_store.py) is, like `handler.py`, a separate
module from `detector.py` — it is the **one place** in this package that
talks to DynamoDB. `detector.py` remains completely AWS-independent; it
has no idea this module exists.

```
lambda_handler(event, context)
    -> detect_telemetry(payload)      # detector.py — unchanged
    -> _log_result(result)             # unchanged: "SentinelGrid detection result: ..."
    -> _persist_if_anomalous(result)    # NEW — see below
    -> return result.to_dict()          # unchanged
```

### What `SentinelGridAnomalyEvents` represents

A dedicated DynamoDB table (defined in
[infrastructure/anomaly-events-storage.yaml](../../infrastructure/anomaly-events-storage.yaml),
**not yet deployed**) holding an auditable history of detected faults —
completely separate from `SentinelGridTelemetry` (raw readings). Nothing
in this step reads from or modifies the telemetry table, its IoT rule, or
the anomaly-detection IoT rule.

### Table key design

- **Partition key**: `node_id` (String)
- **Sort key**: `timestamp` (String) — the same UTC timestamp already on
  every `DetectionResult`.

This lets every anomaly for a given node be queried chronologically,
exactly like `SentinelGridTelemetry`'s own key design — deliberately
consistent with it, even though it's a different table.

### Stored fields

Every item is a near-verbatim copy of `DetectionResult`'s own fields —
`node_id`, `timestamp`, `subsystem`, `fault_type`, `severity`, `reason`,
`evidence`, `detected` — with no schema translation. **No separate
`event_id` field is added**: `node_id` + `timestamp` is already this
table's key and a sufficient identity for one detection; nothing yet
needs to cross-reference these records by a separate id, so adding one
now would be an unused field.

### When an event IS written

When `detect_telemetry()` returns `detected=True` **and** both `node_id`
and `timestamp` are present — they're the table's key, so a record can't
exist without them. In practice this covers all four genuine fault
classifications (`SENSOR_DROPOUT`, `NETWORK_PARTITION`, `DATA_CORRUPTION`,
`NODE_OVERLOAD`) unconditionally, since `detect_telemetry()` only reaches
those after validating `node_id`/`timestamp` are present.

### When an event is NOT written

- `detected=False` (`NORMAL`) — nothing anomalous to record.
- `INVALID_INPUT` results where `node_id` or `timestamp` is itself the
  missing/invalid thing (including an event that failed to parse as JSON
  at all, and therefore has neither) — there is nothing to key the
  record on.
- The one exception: an `INVALID_INPUT` caused by an *unsupported
  subsystem value* on an otherwise well-formed message (`node_id` and
  `timestamp` both present, valid strings) **is** persisted — it's a
  real, identifiable, auditable event about that specific node, even
  though the message itself couldn't be classified further.

### Persistence failure behavior — fail-fast, by design

If `AnomalyEventStore.put_event()` raises `AnomalyEventPersistenceError`
(any real DynamoDB failure), the handler:
1. Logs the failure at `ERROR`/exception level
   (`"SentinelGrid anomaly event persistence FAILED for node_id=... timestamp=..."`).
2. **Re-raises** — it does not catch-and-continue.

This means AWS Lambda reports the invocation itself as failed. Chosen
deliberately for this first implementation: **silently pretending a write
succeeded when it didn't would corrupt the audit trail's completeness
guarantee** — a missing record with no trace is worse than a visibly
failed invocation an operator can retry or investigate. The
already-computed `DetectionResult` and its detection-result log line are
unaffected either way — persistence runs strictly *after* both.

A future step may reconsider this (e.g. a dead-letter queue instead of a
hard failure) once there's an actual operational need — not before.

### Idempotency

Lambda invocations can be retried, so `put_event()` uses a conditional
write:

```python
ConditionExpression="attribute_not_exists(#pk) AND attribute_not_exists(#sk)"
```

Since the primary key is exactly `(node_id, timestamp)`, a retried
invocation for the same detection produces the same key. A
`ConditionalCheckFailedException` from that condition is treated as
**success, not an error** — the event was already recorded; writing it
again would be redundant, not a bug. Anything else DynamoDB can fail
with still raises `AnomalyEventPersistenceError` as normal. No more
elaborate idempotency mechanism (e.g. a separate dedupe table) was
introduced — this one conditional expression is sufficient for what the
key design already guarantees.

### DynamoDB type conversion

boto3's DynamoDB *resource* interface rejects plain Python `float`
values outright and has no representation for `NaN`/`Infinity` at all.
`event_store.py` converts floats to `decimal.Decimal` (DynamoDB's native
numeric type), converts `NaN`/`Infinity` to their string form (so a
`DATA_CORRUPTION` event triggered by one of these can still be recorded
faithfully), and converts tuples (e.g. `evidence["overload_range"]`) to
lists — all recursively, so nested `evidence` structures round-trip
correctly. This is purely a serialization concern; `detector.py`'s
`evidence` values themselves are unchanged.

### boto3 usage note

`event_store.py` is the only module in this package that imports
`boto3`. It is not bundled by
[`package-anomaly-detector.sh`](../../infrastructure/package-anomaly-detector.sh)
(unchanged by this step) — AWS Lambda's Python runtime ships `boto3`
pre-installed, so nothing needs to be vendored into the deployment zip
for this to work once deployed.

### Not yet deployed / not yet wired for real writes

- `infrastructure/anomaly-events-storage.yaml` has **not** been deployed
  — the table does not exist in AWS yet.
- The anomaly detector Lambda's execution role does **not** yet have
  `dynamodb:PutItem` on this table — granting that is a deliberately
  separate, later step (see infrastructure/README.md). Until both of
  these happen, a real invocation reaching `_persist_if_anomalous` would
  fail (as designed — see "Persistence failure behavior" above), not
  silently succeed.
