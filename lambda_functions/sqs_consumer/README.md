# SQS consumer (Step 26)

## Purpose

The missing bridge between Step 25's SQS ingestion buffer and the
existing Analyzer:

```
IoT -> sentinelgrid/telemetry -> SentinelGridTelemetryQueue (SQS) -> [ ??? ] -> Analyzer
```

This package is that bridge: a thin AWS Lambda adapter
(`SentinelGridSqsTelemetryConsumer`) that receives batches of SQS
messages, parses each message body as a telemetry JSON object, and
synchronously invokes the already-deployed
`SentinelGridAnomalyDetector` Lambda with that exact payload — reporting
per-message success/failure back to SQS via the standard AWS Lambda
partial-batch-failure mechanism.

## Why this package does NOT duplicate the Analyzer

The Analyzer (`lambda_functions/anomaly_detector/`) already owns the
**entire** detection -> persistence -> remediation-trigger-submission
pipeline, deployed and live-verified end to end (Step 23). Step 26's
instructions are explicit: there must be exactly **one** Analyzer
implementation. Two architectures were considered for how this consumer
reaches that implementation:

| Option | Description | Verdict |
|---|---|---|
| A — Lambda-to-Lambda invoke | Consumer calls `lambda:InvokeFunction` on the existing, independently-deployed `SentinelGridAnomalyDetector` | **Chosen** |
| B — Embed the Analyzer's code in this package | Copy `detector.py`/`event_store.py`/`handler.py`/`remediation_trigger_client.py` into this package's own zip and call `lambda_handler()` in-process | Rejected |

**Why A was chosen:**

- **One deployed implementation, one code path.** Option B would produce
  *two independently-deployable copies* of the exact same detection/
  persistence/trigger logic (the standalone `SentinelGridAnomalyDetector`
  Lambda, plus a bundled copy inside this consumer's zip) that could
  silently drift apart if one package is redeployed without the other —
  the opposite of "one Analyzer implementation." Option A keeps the
  Analyzer's Lambda as the single place that logic runs, regardless of
  which IoT delivery path (direct, or via this consumer) fed it.
- **Matches the target architecture stated for this step**: `SQS
  Consumer Lambda -> Anomaly Detector` is an explicit invocation
  relationship between two distinct deployed Lambdas, not a code merge.
- **Avoids the Step 22 packaging ripple-effect entirely.**
  `lambda_functions/anomaly_detector/__init__.py` eagerly imports
  `detector.py`, `event_store.py`, `handler.py`, AND
  `remediation_trigger_client.py` — any package that imports anything
  from that package transitively needs all four files (this bit two
  other packaging scripts once, in Step 22). Option A means this
  consumer never imports `lambda_functions.anomaly_detector` at all —
  it invokes the Analyzer purely by ARN, as an opaque service — so that
  entire failure mode does not apply to this package.
- **Keeps the Analyzer independently deployable**, exactly as it already
  is: this step adds a *caller*, not a change to the Analyzer itself.
- **Minimizes IAM.** The consumer's role needs exactly one additional
  permission beyond logging: `lambda:InvokeFunction` scoped to the
  Analyzer's exact ARN (see infrastructure/sqs-consumer.yaml). Option B
  would need no such permission, but at the cost of every principle
  above — this project has consistently favored a scoped, explicit
  permission over an architecture that avoids needing one (see e.g. the
  Detector -> Trigger hand-off, Step 22).

**Trade-off accepted:** Option A adds one synchronous Lambda-to-Lambda
network hop and one extra Lambda invocation per message, compared to
Option B's in-process call. For this project's telemetry volume, this
cost is negligible next to the architectural benefits above.

## Why the invocation is synchronous, not asynchronous

`lambda_functions/anomaly_detector/remediation_trigger_client.py` (the
Detector -> Trigger hand-off, Step 22) uses an **asynchronous**
(`InvocationType="Event"`) invoke, because the Detector only needs to
confirm AWS *accepted* the hand-off — the Trigger's own downstream
success is intentionally outside its visibility.

This consumer cannot make that same choice. AWS Lambda's SQS
partial-batch-failure mechanism requires this function to report, per
message, whether *that message's processing* succeeded — and an
asynchronous invoke only ever confirms AWS accepted the event, never
whether the Analyzer's detection/persistence/trigger-submission
actually succeeded. So `AnomalyDetectorClient.invoke()` uses
`InvocationType="RequestResponse"` and additionally inspects the
response for `FunctionError` (present when the Analyzer's own execution
raised an unhandled exception, e.g. `AnomalyEventPersistenceError` or
`RemediationTriggerSubmissionError`) — both an API-level failure and a
`FunctionError` are treated as this message failing, and reported via
`batchItemFailures`.

## Contract

- **Input** (`event`): the standard AWS Lambda SQS event shape —
  `{"Records": [{"messageId": ..., "body": "<json string>", ...}, ...]}`.
  `body` is **not** wrapped in any envelope (no API Gateway `{"body":
  ...}` shape) — it is exactly the telemetry JSON the IoT rule's SQS
  action wrote to the queue (see `infrastructure/telemetry-sqs-queue.yaml`,
  `UseBase64: false`).
- **Per record**: `body` is parsed as JSON. If parsing fails, or the
  parsed value is not a JSON object, the record is reported as a batch
  item failure — no telemetry-schema-specific validation is duplicated
  here; that is the Analyzer's job via `detect_telemetry()`.
- A successfully parsed payload is passed to
  `AnomalyDetectorClient.invoke(payload)` **exactly as received** —
  `node_id`/`subsystem`/`timestamp`/`status`/`data` are never modified,
  and this function never performs remediation directly.
- **Output**: `{"batchItemFailures": [{"itemIdentifier": messageId}, ...]}`
  — only failed messages appear. An empty `Records` list, or an
  all-succeeded batch, returns `{"batchItemFailures": []}`.
- This function never calls `sqs:DeleteMessage` or otherwise manages
  message acknowledgement directly — the AWS Lambda event source
  mapping does that, based on the returned `batchItemFailures` (see
  infrastructure/sqs-consumer.yaml,
  `FunctionResponseTypes: [ReportBatchItemFailures]`).

## AWS dependency boundary

| | `anomaly_detector_client.py` |
|---|---|
| AWS SDK | The only file in this package that imports `boto3`/`botocore` |
| Network / I/O | Exactly one call per record: `lambda:InvokeFunction` (synchronous) on the Analyzer |
| Detection / persistence / remediation logic | None — entirely delegated to the invoked Analyzer |

`handler.py` owns all SQS-event-shape handling (Records/messageId/body,
`batchItemFailures`) but never talks to AWS itself — it delegates every
AWS call to `anomaly_detector_client.py`, mirroring exactly how
`lambda_functions/anomaly_detector/handler.py` delegates its own AWS
calls to `event_store.py` and `remediation_trigger_client.py`.

## Deployment

See `infrastructure/sqs-consumer.yaml` and
`infrastructure/deploy-sqs-consumer.sh`. Deployed as
`SentinelGridSqsTelemetryConsumer`, triggered by an
`AWS::Lambda::EventSourceMapping` from `SentinelGridTelemetryQueue`.
