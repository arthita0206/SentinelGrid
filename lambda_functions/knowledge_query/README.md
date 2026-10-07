# Knowledge Base Query Interface (Step 35; extended Steps 40, 42, and 43)

Implements SRS FR-6.3: "Provide a query interface for retrieving current
node state and fault/recovery history," extended (Step 43) with SRS
FR-7.1/FR-7.2's read path for CloudWatch metrics. Deployed as the
`SentinelGridKnowledgeQuery` Lambda, fronted by an API Gateway HTTP API
(`SentinelGridKnowledgeQueryApi`, `$default` stage) — see
[infrastructure/knowledge-query.yaml](../../infrastructure/knowledge-query.yaml)
and [infrastructure/README.md](../../infrastructure/README.md)'s
"Knowledge Base Query Interface (Step 35)" / "Fleet Listing & CORS (Step
40)" / "Fleet-Wide Event Feed (Step 42)" / "CloudWatch Metrics Feed (Step
43)" sections for the full infrastructure design.

**Step 40 update**: added `GET /nodes` (a bounded, read-only fleet
listing) and enabled CORS (restricted to one explicit configurable
origin) so the project's frontend (`frontend/`) can call this API from a
browser. Both are additive — all four Step 35 routes/contracts are
unchanged.

**Step 42 update**: added `GET /events` (a bounded, read-only fleet-wide
feed merging `SentinelGridAnomalyEvents` and `SentinelGridRecoveryEvents`
into one normalized, filterable event representation). Additive — all
five prior routes/contracts (Step 35 + Step 40) are unchanged. CORS is
reused as-is, no changes.

**Step 43 update**: added `GET /metrics` (a bounded, read-only feed over
the seven approved `SentinelGrid` CloudWatch metrics `shared/metrics.py`
already publishes). Additive — all six prior routes/contracts (Step 35 +
40 + 42) are unchanged. CORS is reused as-is. This is the FIRST route in
this package backed by CloudWatch rather than DynamoDB — see
`metrics_service.py`, a NEW module (not an addition to `query_service.py`,
which remains DynamoDB-only).

**This interface is strictly read-only. It never modifies the Knowledge
Base or CloudWatch** — see "Read-only guarantee" below.

## Purpose

Steps 33/34 built the persistent Knowledge Base:

- `SentinelGridNodeState` — latest known state, one item per node.
- `SentinelGridAnomalyEvents` — append-only anomaly/fault history.
- `SentinelGridRecoveryEvents` — append-only recovery/escalation history.

Step 28 built the CloudWatch metric contract (`shared/metrics.py`) —
seven approved `SentinelGrid`-namespace metrics, published by the
Analyzer/Executor/Planner/Verifier Lambdas.

Step 35 provides the first way to *read* the Knowledge Base without going
through the AWS CLI/console directly; Step 43 does the same for the
CloudWatch metrics. Both are new, independent read paths — neither is
wired into the telemetry or remediation write pipeline in any way, and
neither can write to what it reads (DynamoDB or CloudWatch, respectively).

```
API Gateway HTTP API event
    -> handler.lambda_handler
    -> route dispatch + validation (node_id / limit / order / .. / start / end / fault_type)
    -> query_service.KnowledgeQueryService (DynamoDB routes)
       OR metrics_service.MetricsQueryService (GET /metrics only)
    -> DynamoDB: GetItem/Query/Scan  OR  CloudWatch: GetMetricData
    -> JSON response
```

## Structure

- [`handler.py`](handler.py) — the ONLY module here that adapts to
  API Gateway's event/response shape. No DynamoDB or CloudWatch logic of
  its own — HTTP/routing concerns only.
- [`query_service.py`](query_service.py) — the ONLY module here that
  talks to DynamoDB. All DynamoDB reads live here, fully testable without
  live AWS via constructor-injected Table resources.
- [`metrics_service.py`](metrics_service.py) — **(Step 43)** the ONLY
  module here that talks to CloudWatch. All `GET /metrics` logic lives
  here, fully testable without live AWS via a constructor-injected
  CloudWatch client. A separate module from `query_service.py`
  deliberately — DynamoDB and CloudWatch are two independent AWS
  services with two independent read paths, kept in two independent
  files, exactly mirroring how `shared/metrics.py` (CloudWatch *writes*)
  is already separate from `shared/node_state_store.py`/`event_store.py`
  (DynamoDB writes) elsewhere in this project.

This mirrors every other AWS-facing module split in this project (e.g.
`anomaly_detector/handler.py` + `event_store.py`).

## Endpoints

| Method | Path | Returns |
|---|---|---|
| `GET` | `/nodes` | **(Step 40)** Bounded fleet listing — up to `limit` current-state items. |
| `GET` | `/events` | **(Step 42)** Bounded, merged anomaly + recovery event feed. |
| `GET` | `/metrics` | **(Step 43)** Bounded CloudWatch feed over the 7 approved SentinelGrid metrics. |
| `GET` | `/nodes/{node_id}` | Current state. |
| `GET` | `/nodes/{node_id}/anomalies` | Anomaly history. |
| `GET` | `/nodes/{node_id}/recoveries` | Recovery history. |
| `GET` | `/nodes/{node_id}/summary` | Current state + both histories, combined. |

No other route exists. There is no POST/PUT/PATCH/DELETE route anywhere
in the API Gateway HTTP API this Lambda is attached to.

### `GET /nodes` (Step 40 — fleet listing)

Query parameters: `limit` (default `50`, min `1`, max `500`), `order`
(`asc`/`desc`, default `desc`), and optionally `subsystem`
(`power`/`water`/`traffic`) and `state` (`HEALTHY`/`FAULT_DETECTED`/
`RECOVERING`/`RECOVERED`/`ESCALATION_REQUIRED`/`UNKNOWN`).

`200`:
```json
{"nodes": [{"node_id": "power-001", "subsystem": "power", "status": "ONLINE", "state": "HEALTHY", "last_telemetry_timestamp": "...", "last_state_update": "...", "reason": "..."}], "count": 1, "limit": 50, "order": "desc"}
```
`subsystem`/`state` are echoed back into the response only when a filter
was actually supplied.

**Implementation — read this before assuming more than it says:**
- Uses a single, bounded DynamoDB `Scan` (`Limit` always ≤ `MAX_LIST_LIMIT`
  = 500, never caller-controlled beyond that) — `SentinelGridNodeState`
  has no secondary index a fleet-wide read could `Query` against instead
  (its only key is `node_id`), and creating a GSI was judged unnecessary
  infrastructure for this project's scale (up to ~500 nodes, SRS NFR-3).
- **`order` sorts the returned page by `node_id` (lexicographic) — NOT
  by telemetry recency.** DynamoDB `Scan` has no native ordering
  guarantee at all; this Lambda sorts the already-bounded page in Python
  after the Scan completes, which is safe (cheap, ≤500 items) but is
  emphatically not a claim about "newest first" the way `order` means
  for the two history routes (which DO have a real sort key). Never
  present `GET /nodes`'s `order` as recency ordering.
- **Filtering caveat**: `subsystem`/`state` are applied as a DynamoDB
  `FilterExpression` on top of the same bounded Scan — safe (both reach
  DynamoDB only as `ExpressionAttributeValues`, never string-interpolated)
  but NOT complete at scale: `Limit` bounds items *examined*, and the
  filter only applies to that already-limited window, so a filtered
  response can under-count real matches (or return zero) even when more
  matching nodes exist beyond what one Scan call examined. This is a
  real, live-verified limitation (see infrastructure/README.md's Step 40
  section for the exact live evidence), not a bug — a caller needing a
  guaranteed-complete filtered fleet view is not what `GET /nodes`
  provides.
- Uses `GetItem`/`Scan` — never `Query` (there is nothing to `Query` a
  fleet-wide read against). Never `Query` on this route; never `Scan` on
  either history table.

### `GET /events` (Step 42 — fleet-wide event feed)

Query parameters: `limit` (default `50`, min `1`, max `100` — **a
separate, smaller bound than `GET /nodes`'s 1-500**, its own
`DEFAULT_EVENT_LIMIT`/`MAX_EVENT_LIMIT` constants, never shared/conflated
with `MAX_LIST_LIMIT` or the per-node history routes' `MAX_LIMIT`),
`order` (`asc`/`desc`, default `desc`), and optionally `event_type`
(`anomaly`/`recovery`), `subsystem` (`power`/`water`/`traffic`),
`severity` (`NORMAL`/`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`), and `status`
(`SUCCESS`/`FAILURE`/`ESCALATION_REQUIRED` — **recovery events only**,
see below).

`200`:
```json
{"events": [{"event_type": "anomaly", "node_id": "power-001", "timestamp": "2026-01-01T00:00:05+00:00", "subsystem": "power", "fault_type": "NODE_OVERLOAD", "severity": "MEDIUM", "reason": "...", "detected": true, "evidence": {}, "action": null, "automatic": null, "status": null, "success": null, "recovery_started_at": null, "recorded_at": null}], "count": 1, "limit": 50, "order": "desc"}
```
`event_type`/`subsystem`/`severity`/`status` are echoed back into the
response only when that filter was actually supplied.

**Unified event shape**: every item — whether sourced from
`SentinelGridAnomalyEvents` or `SentinelGridRecoveryEvents` — is
normalized onto one flat superset schema. Fields that don't apply to a
given `event_type` are always `null` (never fabricated, never omitted):
anomaly events carry `detected`/`evidence` and leave `action`/
`automatic`/`status`/`success`/`recovery_started_at`/`recorded_at` as
`null`; recovery events carry the latter and leave `detected`/`evidence`
as `null`. See `_normalize_anomaly_event`/`_normalize_recovery_event` in
`query_service.py` for the exact field-by-field mapping.

**Critical timestamp-semantics rule**: a recovery event's own
`timestamp` (its DynamoDB sort key) is carried through **unchanged** and
is its primary/identity timestamp — it is NEVER relabeled as "detection
time" anywhere in this codebase, even though by this project's own
`recovery_event_store.py` design it is often numerically equal to the
originating anomaly's detection timestamp. `recovery_started_at`/
`recorded_at` are separate, distinct fields carried through as-is.

**Implementation — read this before assuming more than it says:**
- **Not a fleet-wide index, not a globally-complete query.** Neither
  `SentinelGridAnomalyEvents` nor `SentinelGridRecoveryEvents` has a
  fleet-wide (cross-`node_id`) secondary index — both are keyed by
  `(node_id, timestamp)` only. There is no way to `Query` "the N most
  recent events across the whole fleet" directly. This route instead
  independently `Scan`s each table up to a fixed **internal** bound
  (`_EVENT_TABLE_SCAN_LIMIT = 500` per table — not client-controlled,
  reusing the same SRS NFR-3 (~500-node) justification as `MAX_LIST_LIMIT`
  in `GET /nodes`), normalizes and merges the two result sets in Python,
  sorts the merged list by each event's own `timestamp`, and truncates to
  the client-facing `limit`.
- **Be precise about what this guarantees**: once either table holds more
  than `_EVENT_TABLE_SCAN_LIMIT` items, a single `Scan` call is not
  guaranteed to examine the fleet's true most-recent items first —
  DynamoDB `Scan` has no ordering guarantee over which items a bounded
  `Limit` returns. This route is honestly a **bounded, recent-ish
  operations feed**, not a complete, provably-correct historical
  time-series query. Do not present it as one. (At this project's current
  table sizes — tens of items per table, see
  infrastructure/README.md's Step 42 section for live counts — this
  limitation has no practical effect today, but the code and this
  document do not rely on table size staying small to remain honest.)
- `event_type` narrows which table(s) are even `Scan`ned: `anomaly` skips
  the recovery table's `Scan` entirely (and vice versa) — not merely a
  post-fetch filter, a real cost saving verified in
  `tests/test_knowledge_query_service.py`.
- `subsystem`/`severity` are applied as a DynamoDB `FilterExpression` on
  **both** underlying `Scan` calls (same safe, non-string-interpolated
  `ExpressionAttributeValues` pattern as `GET /nodes`, and the same
  "bounds items *examined*, not a guaranteed-complete filtered count"
  caveat applies).
- `status` is a **recovery-only concept** — no such attribute exists on
  `SentinelGridAnomalyEvents` items, so it is never even attempted against
  the anomaly table's `Scan`. Combining `event_type=anomaly` with any
  `status` value is rejected outright with `400 INVALID_STATUS` (checked
  in `handler.py` before the service layer is ever called) — rather than
  silently ignoring `status` or silently narrowing to recovery-only
  results without telling the caller.
- Uses `Scan` (added to both tables' IAM policy Step 42, alongside the
  existing `Query` grant each already had for the per-node history
  routes) — never any write action.

### `GET /metrics` (Step 43 — CloudWatch metrics feed)

Query parameters: `start`/`end` (ISO-8601, both optional — default: the
last `3600` seconds ending now; if only one is given, the other is
derived the same way), `subsystem` (`power`/`water`/`traffic`), and
`fault_type` (`SENSOR_DROPOUT`/`NETWORK_PARTITION`/`DATA_CORRUPTION`/
`NODE_OVERLOAD`/`INVALID_INPUT` — **excludes** `detector.py`'s
`FaultType.NONE`, since metrics are never published with that value).

`200`:
```json
{"namespace": "SentinelGrid", "start": "2026-01-01T00:00:00+00:00", "end": "2026-01-01T01:00:00+00:00", "period_seconds": 60, "series": [{"metric_name": "FaultDetectionCount", "unit": "Count", "dimensions": {"Subsystem": "power", "FaultType": "NODE_OVERLOAD"}, "datapoints": [{"timestamp": "2026-01-01T00:30:00+00:00", "value": 3}], "summary": {"datapoint_count": 1, "sum": 3}}]}
```
`subsystem`/`fault_type` are echoed back into the response only when a
filter was actually supplied.

**Approved-metric allowlist — never an arbitrary CloudWatch query**: only
the 7 metrics in `metrics_service.APPROVED_METRICS`
(`FaultDetectionCount`, `DetectionLatency`, `RecoveryAttemptCount`,
`RecoverySuccessCount`, `RecoveryFailureCount`, `RecoveryLatency`,
`UnresolvedFaultCount`), only the `SentinelGrid` namespace, only the
`Subsystem`/`FaultType` dimensions — there is no request parameter that
accepts an arbitrary `MetricName` or `Namespace`, and none will ever be
added without a corresponding allowlist change.

**Time bounds**: default window 1 hour (`3600` seconds), maximum window
24 hours (`86400` seconds) — enforced in `handler.py`, not merely
suggested. Rejected with `400 TIME_RANGE_TOO_LARGE` if exceeded, `400
INVALID_TIME_RANGE` if `end <= start`, `400 FUTURE_END_TIMESTAMP` if
`end` is more than 60 seconds in the future (a small clock-skew
allowance, not a loophole), `400 INVALID_START`/`400 INVALID_END` for an
unparseable timestamp.

**Implementation — read this before assuming more than it says:**
- **No `cloudwatch:ListMetrics` call, no extra IAM permission for it.**
  Which (Subsystem, FaultType) dimension pairs to query is derived from
  this project's own closed, already-known vocabulary (3 subsystems x 5
  fault types = 15 pairs, narrowed by whichever of `subsystem`/
  `fault_type` the caller supplied) rather than discovered via
  `ListMetrics` — see `metrics_service.py`'s own docstring for the full
  reasoning. At most 105 `MetricDataQuery` entries (15 pairs x 7
  metrics) are built, well under `GetMetricData`'s own 500-query-per-call
  limit — one bounded `GetMetricData` call, never paginated, never
  looped.
- **CloudWatch period scales with the requested window, never claiming
  sub-minute precision**: 60 seconds for windows <= 1 hour, 300 seconds
  for windows <= 6 hours, 900 seconds for windows <= 24 hours. This is
  purely to bound how many datapoints one response returns — the
  underlying metrics genuinely publish at roughly 60-second resolution
  (each `shared/metrics.py` `put_metric_data` call is one raw
  observation; CloudWatch aggregates same-period observations
  automatically), so a 60-second period is always honest, and a larger
  period for a longer window is a real CloudWatch-side pre-aggregation
  (`Sum`/`Average` across that wider bucket), not a fabricated finer
  reading.
- **Statistic per metric**: `Sum` for Count-unit metrics (total
  observations per period bucket), `Average` for Milliseconds-unit
  metrics (mean of whatever raw latency observations landed in that
  bucket). Never a percentile — this project's data does not support
  one.
- **"No datapoint" is not "zero" — the empty-series rule**: when BOTH
  `subsystem` AND `fault_type` are given (a single, fully-specified
  dimension pair), all 7 approved metrics are returned for that exact
  pair, even with an empty `datapoints` array — an honest "no data in
  this window," never a fabricated zero, never a silently omitted
  metric. When `subsystem`/`fault_type` is absent or only partially
  given, this route is effectively DISCOVERING "the available
  SentinelGrid metric series for the requested window" across multiple
  unknown dimension pairs — a series with zero datapoints in that case
  is simply omitted from the response (see `metrics_service.py`'s
  `get_metrics()` docstring for the exact reasoning).
- **Optional `summary`**: present only when a series has >=1 datapoint —
  `{"datapoint_count": N, "sum": X}` for Count metrics, `{"datapoint_count":
  N, "average": X}` for Milliseconds metrics. Never a percentile, and an
  `average` is always labeled "average" (in the JSON key and in the
  frontend's own rendering) — never "MTTR." See "Metric semantics" below
  for why.
- Uses `cloudwatch:GetMetricData` only — never `PutMetricData`,
  `PutMetricAlarm`, `SetAlarmState`, `DeleteAlarms`, or
  `GetMetricStatistics`.

### `GET /nodes/{node_id}`

`200`:
```json
{"node": {"node_id": "power-001", "subsystem": "power", "status": "ONLINE", "state": "HEALTHY", "last_telemetry_timestamp": "2026-01-01T00:00:00+00:00", "last_state_update": "2026-01-01T00:00:01+00:00", "reason": "..."}}
```

`404` if no current-state item exists for `node_id`:
```json
{"error": "NODE_NOT_FOUND", "message": "No current state exists for the requested node."}
```

Uses `GetItem` (never `Scan`) — `node_id` is the table's sole key.

### `GET /nodes/{node_id}/anomalies` and `GET /nodes/{node_id}/recoveries`

Query parameters: `limit` (default `20`, min `1`, max `100`) and `order`
(`asc`/`desc`, default `desc`).

`200`:
```json
{"node_id": "power-001", "count": 2, "items": [{"node_id": "power-001", "timestamp": "...", "subsystem": "power", "fault_type": "NODE_OVERLOAD", "severity": "MEDIUM", "reason": "...", "evidence": {}, "detected": true}]}
```

Uses `Query` (never `Scan`), with DynamoDB's own `ScanIndexForward`
(ordering) and `Limit` — never fetched in full and re-sorted/truncated in
Python.

### `GET /nodes/{node_id}/summary`

Same `limit`/`order` parameters, applied to both history sections.

`200`:
```json
{"node_id": "power-001", "current_state": {"...": "..."}, "anomalies": {"count": 2, "items": [...]}, "recoveries": {"count": 1, "items": [...]}}
```

`404` (same shape as above) if no current-state item exists — a summary
is never fabricated from partial data when the node itself is unknown.

## Request validation

- `node_id`: required (for the four per-node routes only — `GET /nodes`
  and `GET /events` have none), string, trimmed, non-empty, max 128
  characters. Rejected with `400 INVALID_NODE_ID`. Never used to build a
  DynamoDB expression string — always passed as an
  `ExpressionAttributeValues` placeholder, so its content cannot inject
  DynamoDB syntax.
- `limit`: optional integer. `1`-`100` for the three per-node history
  routes AND for `GET /events` (Step 42 — coincidentally the same range
  as the history routes, but independently validated via its own
  `DEFAULT_EVENT_LIMIT`/`MAX_EVENT_LIMIT` constants, not shared with
  `MAX_LIMIT`); **`1`-`500` for `GET /nodes`** (Step 40 — a separate,
  larger bound). Rejected with `400 INVALID_LIMIT` if non-integer or out
  of range — never silently clamped.
- `order`: optional, `asc` or `desc`. Rejected with `400 INVALID_ORDER`
  otherwise. See `GET /nodes`'s own section above for what `order` means
  there specifically (node_id sort, not recency); for `GET /events`,
  `order` sorts by each event's own `timestamp`.
- `subsystem`/`state` (Step 40, `GET /nodes` only): optional, must
  exactly match one of this project's own closed vocabularies
  (`power`/`water`/`traffic`; `HEALTHY`/`FAULT_DETECTED`/`RECOVERING`/
  `RECOVERED`/`ESCALATION_REQUIRED`/`UNKNOWN`). Rejected with
  `400 INVALID_SUBSYSTEM`/`400 INVALID_STATE` otherwise.
- `event_type`/`subsystem`/`severity`/`status` (Step 42, `GET /events`
  only): optional, must exactly match one of this project's own closed
  vocabularies (`anomaly`/`recovery`; `power`/`water`/`traffic`;
  `NORMAL`/`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`;
  `SUCCESS`/`FAILURE`/`ESCALATION_REQUIRED`). Rejected with
  `400 INVALID_EVENT_TYPE`/`400 INVALID_SUBSYSTEM`/`400 INVALID_SEVERITY`/
  `400 INVALID_STATUS` otherwise. `status` combined with
  `event_type=anomaly` is additionally rejected with `400 INVALID_STATUS`
  even when `status` itself is individually valid, since `status` is a
  recovery-only concept (see `GET /events`'s own section above).
- `start`/`end` (Step 43, `GET /metrics` only): optional ISO-8601
  timestamps. Rejected with `400 INVALID_START`/`400 INVALID_END` if
  unparseable, `400 INVALID_TIME_RANGE` if `end <= start`,
  `400 TIME_RANGE_TOO_LARGE` if the resolved window exceeds 24 hours,
  `400 FUTURE_END_TIMESTAMP` if `end` is more than 60 seconds in the
  future. See `GET /metrics`'s own section above for the full
  default/derivation rules.
- `fault_type` (Step 43, `GET /metrics` only): optional, must exactly
  match one of `SENSOR_DROPOUT`/`NETWORK_PARTITION`/`DATA_CORRUPTION`/
  `NODE_OVERLOAD`/`INVALID_INPUT` — **deliberately excludes**
  `detector.py`'s `FaultType.NONE`. Rejected with `400
  INVALID_FAULT_TYPE` otherwise. `subsystem` for `GET /metrics` reuses
  the exact same validator/vocabulary as `GET /nodes`'s own `subsystem`
  filter.

No table name, projection expression, arbitrary filter expression,
CloudWatch namespace, or CloudWatch metric name is ever accepted from a
request — `subsystem`/`state`/`event_type`/`severity`/`status`/
`fault_type` select which fixed, pre-built filter clause or dimension
value (if any) is used; they cannot introduce new expression syntax or
query anything outside this package's own fixed contracts.

## Error responses

| Status | When |
|---|---|
| `400` | Invalid `node_id`/`limit`/`order`/`subsystem`/`state`/`event_type`/`severity`/`status`/`start`/`end`/`fault_type` (including the `event_type=anomaly` + `status` contradiction), or an unrecognized route (defensive only — API Gateway itself blocks any request that doesn't match one of the seven defined GET routes). |
| `404` | No current-state item exists for the requested `node_id` (per-node routes only — `GET /nodes`, `GET /events`, and `GET /metrics` return `200` with an empty array/series when nothing matches, never `404`). |
| `500` | A genuine DynamoDB failure (`KnowledgeQueryError`), a genuine CloudWatch failure (`MetricsQueryError`), or any other unexpected exception. |

There is no `503` in this implementation — this Lambda has two dependency
classes (DynamoDB reads, CloudWatch metric reads) and no
currently-implemented way to distinguish a transient dependency outage
from any other failure in either, so a `503` would be an unverified
distinction rather than a real one.

Every error response is `{"error": "<CODE>", "message": "<safe text>"}`.
No response body ever includes a stack trace, a boto3 exception's raw
text, an AWS request ID, an IAM detail, or a table name — full detail is
logged to CloudWatch (`logger.exception`/`logger.error`) but never
returned to the caller.

## Read-only guarantee

`query_service.py` contains no call to `put_item`/`update_item`/
`delete_item`/`batch_write_item` anywhere — enforced by a dedicated
static source-scan test
(`tests/test_knowledge_query_service.py::test_query_service_module_never_calls_any_write_operation`)
that fails the build the moment such a call is ever added. `metrics_service.py`
(Step 43) contains no call to `put_metric_data`/`put_metric_alarm`/
`set_alarm_state`/`delete_alarms` anywhere either — same guarantee, its
own dedicated static source-scan test
(`tests/test_knowledge_query_metrics_service.py::test_metrics_service_module_never_calls_a_write_or_alarm_operation`).
This is on top of IAM only granting `dynamodb:GetItem`/`dynamodb:Scan`/
`dynamodb:Query` (Step 40 added `Scan`, scoped to `SentinelGridNodeState`
only; Step 42 added `Scan` to `SentinelGridAnomalyEvents`/
`SentinelGridRecoveryEvents` too, alongside each table's existing `Query`
grant — still scoped to exactly those three table ARNs, never a
wildcard) and `cloudwatch:GetMetricData` (Step 43, `Resource: "*"` — see
"IAM" below for why no narrower resource or condition is possible for
this specific action — see infrastructure/README.md's IAM section) —
independent layers, none of which ever grant a write action of any kind,
guarantee this Lambda can never modify the Knowledge Base or CloudWatch.

## Dependency injection / testability

`KnowledgeQueryService` accepts optional pre-built DynamoDB Table
resources (`node_state_table=`, `anomaly_events_table=`,
`recovery_events_table=`) — tests inject `MagicMock()` instances, so no
real AWS call is ever made during `pytest`. `MetricsQueryService` (Step
43) accepts an optional pre-built CloudWatch client (`client=`) for the
same reason. Constructing either service never touches AWS by itself
(lazy boto3 initialization) — only the first actual query does. See
`tests/test_knowledge_query_service.py`, `tests/test_knowledge_query_metrics_service.py`,
and `tests/test_knowledge_query_handler.py`.

## Security / authentication limitation

**No authentication or authorization is implemented — still true after
Step 40.** This is an explicitly documented limitation (see
infrastructure/README.md's "Security / authentication limitation"
subsection), not an oversight — adding Cognito or a custom authorizer was
judged a substantial new subsystem, out of scope for both Step 35 and
Step 40 given no architectural blocker required it. Enabling CORS (below)
makes this API reachable from a browser tab; it does not add
authentication. The interface's read-only nature (see above) bounds the
impact, but the HTTP API endpoint is public/unauthenticated once
deployed. **Do not treat this as a production-ready public API.**

## CORS (Step 40)

**Now enabled** — restricted to one explicit, configurable origin (the
`FrontendOrigin` CloudFormation parameter, defaulting to
`http://localhost:5173`, the frontend's local Vite dev server), never a
wildcard (`"*"`). Configured entirely at the API Gateway HTTP API level
(`KnowledgeQueryApi.CorsConfiguration` in
`infrastructure/knowledge-query.yaml`) — this Lambda's own code adds no
`Access-Control-*` header itself; API Gateway handles the CORS preflight
`OPTIONS` request without ever invoking this Lambda. Only `GET` is
allowed (there is no other method on this API to allow). No credentials/
cookies are used by this API, so `AllowCredentials` is correctly omitted.
See infrastructure/README.md's "Fleet Listing & CORS (Step 40)" section
for the full configuration and live verification evidence (both the
preflight response and the actual `GET` response were confirmed, live,
to carry the correct `Access-Control-Allow-Origin` header for the
configured origin). **Step 42 makes no CORS changes at all** — `GET
/events` is covered by this same API-level configuration automatically
(CORS is per-API, not per-route), re-verified live for `/events`
specifically (see infrastructure/README.md's "Fleet-Wide Event Feed
(Step 42)" section). **Step 43 makes no CORS changes either** — `GET
/metrics` is covered the same way, re-verified live for `/metrics`
specifically (see infrastructure/README.md's "CloudWatch Metrics Feed
(Step 43)" section).

## IAM for GET /metrics — why Resource must be "*" with no Condition

Unlike the DynamoDB routes (each grant scoped to one exact table ARN via
`!ImportValue`), `cloudwatch:GetMetricData` has no ARN-level resource
scoping at all — `Resource: "*"` is the only value AWS accepts for this
action, regardless of how narrow the actual query is.

An earlier version of this policy also added a `cloudwatch:namespace`
`Condition` (mirroring this project's own Step 28 `PutMetricData`
grants, which use that exact condition key effectively). **Live
evidence showed this does NOT work for `GetMetricData`**:
`aws iam simulate-principal-policy` against the deployed role returned
`implicitDeny` with `MissingContextValues: [cloudwatch:namespace]` when
no context was supplied (matching what a real `GetMetricData` call
actually sends), and the deployed Lambda's own CloudWatch Logs showed
the identical `AccessDenied` for every real request until the condition
was removed. The reason: `PutMetricData`/`ListMetrics`/
`GetMetricStatistics` each have a top-level `Namespace` request
parameter that AWS surfaces as the `cloudwatch:namespace` IAM condition
context key — `GetMetricData` instead expresses namespace nested inside
each `MetricDataQueries[].MetricStat.Metric.Namespace` entry, which AWS
does not surface as that context key at all.

**Practical consequence**: IAM cannot narrow this specific grant beyond
`Resource: "*"` with no effective condition. The application code is
therefore the sole enforcement that only the `SentinelGrid` namespace and
only the 7 approved metric names are ever queried —
`metrics_service.py`'s `NAMESPACE`/`APPROVED_METRICS` constants are fixed
and never caller-controlled (there is no `namespace`/`metric_name` query
parameter on `GET /metrics` at all — see that route's own section
above). This is a genuine, documented AWS API limitation, not an
oversight or a shortcut.

## Metric semantics (Step 43 — read before presenting any of these numbers)

These are the SAME metrics/semantics `infrastructure/README.md`'s
"CloudWatch metrics (Step 28)" section already documents in full — this
section restates the parts most relevant to a caller of `GET /metrics`,
and does not relax or reinterpret any of them:

- **All 7 metrics are raw, observational CloudWatch measurements** —
  never a computed SLA/accuracy figure, never smoothed, never
  extrapolated.
- **`DetectionLatency`**: raw detection-latency measurements (telemetry
  timestamp to analyzer detection). Not an aggregate SLA guarantee.
- **`RecoveryLatency`**: detection timestamp to recovery completion —
  this COMBINES detection AND remediation time. It is NOT isolated
  remediation execution time, and this API/frontend never presents its
  average as "MTTR" — only as "Average," consistently, everywhere it is
  shown (`GET /metrics`'s own `summary.average` field, and the
  frontend's `MetricChartCard`).
- **`UnresolvedFaultCount`**: an unresolved/escalation OBSERVATION
  COUNT — not a continuously-maintained active-fault gauge. It never
  decrements (see infrastructure/README.md's "UnresolvedFaultCount
  ownership" subsection for the full reasoning), so an OK CloudWatch
  alarm state elsewhere does not mean a fault counted here was resolved.
- **`FaultDetectionCount`**: count of detection observations (Analyzer
  invocations where `detected=True`).
- **`RecoveryAttemptCount`**/**`RecoverySuccessCount`**/
  **`RecoveryFailureCount`**: counts of remediation attempts/successes/
  failures respectively — see infrastructure/README.md for the exact
  Executor-side conditions each is published under.
- **No detection-accuracy or false-positive-rate figure is computed or
  exposed anywhere in this route** — this project has no labeled
  ground-truth dataset sufficient to calculate either, and `GET /metrics`
  does not invent one (see infrastructure/README.md's own "Detection
  accuracy / false-positive rate — not implemented in Step 28" section,
  which remains true unchanged).
- **No percentile (p50/p95/p99) is ever computed** — only `sum` (Count
  metrics) or `average` (Milliseconds metrics), both over whatever
  datapoints CloudWatch actually returned for the requested window.

## Distinction between current state and historical events

`SentinelGridNodeState` is the *latest known* snapshot — one item per
node, always overwritten in place, never appended to.
`SentinelGridAnomalyEvents`/`SentinelGridRecoveryEvents` are append-only
historical records. This query interface never treats the state table as
history, and never treats the event tables as a current-state model —
see infrastructure/README.md's Step 33/34 sections for the full reasoning
behind keeping them separate.

## What this package does NOT implement

No authentication/authorization beyond what's described above, no
MTTR/detection-accuracy/false-positive-rate computation, no 500-node
scale validation, no NFR-1/NFR-2 measurement, no dashboard integration,
no complete/guaranteed-correct fleet-wide historical event query (see
`GET /events`'s own section above), no GSI/fleet-wide index of any kind,
no `cloudwatch:ListMetrics` call, no CloudWatch alarm/dashboard/SNS
modification of any kind, no metric publication (`GET /metrics` is
strictly read-only — see "Read-only guarantee" above), no percentile
computation. This package implements exactly FR-6.3 (Steps 35/40/42) and
the read path for FR-7.1/FR-7.2 (Step 43).
