/**
 * Types matching the deployed SentinelGridKnowledgeQuery API's exact
 * response shapes — see lambda_functions/knowledge_query/README.md for
 * the authoritative documentation these are transcribed from. Kept in
 * sync BY HAND (the API has no OpenAPI/schema export today); if the
 * backend response shape ever changes, update this file and
 * src/services/api.ts together.
 */

import type { FaultType, NodeState, RecoveryStatus, Severity, Subsystem, TelemetryStatus } from './domain';

/** GET /nodes/{node_id} -> 200 body's "node" field, and the
 * "current_state" field of a summary response. */
export interface NodeCurrentState {
  node_id: string;
  subsystem: Subsystem;
  status: TelemetryStatus;
  state: NodeState;
  last_telemetry_timestamp: string;
  last_state_update: string;
  reason: string;
  fault_type?: FaultType | null;
  fault_severity?: Severity | null;
}

/** One item of GET /nodes/{node_id}/anomalies. */
export interface AnomalyEvent {
  node_id: string;
  timestamp: string;
  subsystem: Subsystem;
  fault_type: FaultType;
  severity: Severity;
  reason: string;
  evidence: Record<string, unknown>;
  detected: boolean;
}

/** One item of GET /nodes/{node_id}/recoveries. */
export interface RecoveryEvent {
  node_id: string;
  timestamp: string;
  subsystem: Subsystem;
  fault_type: FaultType;
  severity: Severity;
  action: string;
  automatic: boolean;
  status: RecoveryStatus;
  success: boolean;
  reason: string;
  recovery_started_at: string;
  recorded_at: string;
}

export interface NodeCurrentStateResponse {
  node: NodeCurrentState;
}

export interface AnomalyHistoryResponse {
  node_id: string;
  count: number;
  items: AnomalyEvent[];
}

export interface RecoveryHistoryResponse {
  node_id: string;
  count: number;
  items: RecoveryEvent[];
}

export interface NodeSummaryResponse {
  node_id: string;
  current_state: NodeCurrentState;
  anomalies: { count: number; items: AnomalyEvent[] };
  recoveries: { count: number; items: RecoveryEvent[] };
}

/** The API's uniform error body shape (400/404/500 — see the README's
 * "Error responses" table). */
export interface ApiErrorBody {
  error: string;
  message: string;
}

export interface HistoryQueryOptions {
  limit?: number;
  order?: 'asc' | 'desc';
}

/** GET /nodes -> 200 body (Step 40 — SRS FR-6.1/FR-6.3 fleet listing).
 *
 * IMPORTANT semantics, carried over exactly from the backend
 * (lambda_functions/knowledge_query/query_service.py's `list_nodes()`
 * docstring — read that before assuming more than this says):
 *   - `order` sorts the returned page by `node_id` (lexicographic) —
 *     NOT by telemetry recency. The backend table has no sort key
 *     across the fleet, so there is no server-side "newest first".
 *   - This is a BOUNDED read (`limit`, max 500) via a DynamoDB `Scan` —
 *     not a paginated, guaranteed-complete listing. `count` reflects
 *     only what this one call actually returned.
 *   - If `subsystem`/`state` filters were applied, `count`/`nodes` may
 *     under-represent the true number of matching nodes beyond what
 *     this one bounded Scan examined — see the backend docstring's own
 *     "Filtering caveat" for why, and never present a filtered `count`
 *     here as an exhaustive total.
 */
export interface NodeListResponse {
  nodes: NodeCurrentState[];
  count: number;
  limit: number;
  order: 'asc' | 'desc';
  subsystem?: Subsystem;
  state?: NodeState;
}

export interface ListNodesQueryOptions {
  limit?: number;
  order?: 'asc' | 'desc';
  subsystem?: Subsystem;
  state?: NodeState;
}

/** GET /events -> one item of the "events" array (Step 42 — fleet-wide
 * event feed). Matches `lambda_functions/knowledge_query/query_service.py`'s
 * `_normalize_anomaly_event`/`_normalize_recovery_event` output exactly:
 * a flat shape covering both event kinds, with `null` (never a
 * fabricated value) for whichever fields don't apply to a given
 * `event_type`.
 *
 * IMPORTANT — `timestamp` semantics (read before using this for
 * anything): for an `event_type: 'anomaly'` item, `timestamp` is that
 * anomaly's own detection timestamp. For an `event_type: 'recovery'`
 * item, `timestamp` is `SentinelGridRecoveryEvents`' own `timestamp`
 * attribute (its sort key) — carried through UNCHANGED, never relabeled
 * as "detection time" here even though it is often equal to the
 * originating detection's timestamp by backend construction. Use
 * `recovery_started_at`/`recorded_at` (recovery events only) for the
 * other two timestamps `SentinelGridRecoveryEvents` separately tracks.
 */
export interface FleetEvent {
  event_type: 'anomaly' | 'recovery';
  node_id: string;
  timestamp: string;
  subsystem: Subsystem;
  fault_type: FaultType;
  severity: Severity;
  reason: string;
  /** anomaly events only */
  detected: boolean | null;
  /** anomaly events only */
  evidence: Record<string, unknown> | null;
  /** recovery events only */
  action: string | null;
  /** recovery events only */
  automatic: boolean | null;
  /** recovery events only */
  status: RecoveryStatus | null;
  /** recovery events only */
  success: boolean | null;
  /** recovery events only */
  recovery_started_at: string | null;
  /** recovery events only */
  recorded_at: string | null;
}

/** GET /events -> 200 body.
 *
 * IMPORTANT semantics — read before assuming this is complete history:
 *   - This is a BOUNDED, RECENT-ish event feed (`limit`, max 100), never
 *     a complete historical export. Both underlying tables
 *     (`SentinelGridAnomalyEvents`/`SentinelGridRecoveryEvents`) are
 *     keyed `(node_id, timestamp)` with NO fleet-wide timestamp index —
 *     there is no way to `Query` "all events across all nodes,
 *     chronologically" directly. The backend performs a bounded `Scan`
 *     per table (internal limit, not client-controlled), merges the two
 *     result sets, and sorts them in application code by each event's
 *     own `timestamp` — see `FleetEvent`'s own doc comment and
 *     `lambda_functions/knowledge_query/query_service.py`'s
 *     `list_events()` docstring for the full "why Scan, what it does and
 *     does not guarantee" reasoning.
 *   - `order` sorts by each event's own `timestamp` (descending =
 *     newest-timestamp-first among whatever was examined, the default).
 *   - `count` reflects only what THIS call actually returned — never
 *     present it as a total historical event count.
 */
export interface EventListResponse {
  events: FleetEvent[];
  count: number;
  limit: number;
  order: 'asc' | 'desc';
  event_type?: 'anomaly' | 'recovery';
  subsystem?: Subsystem;
  severity?: Severity;
  status?: RecoveryStatus;
}

export interface ListEventsQueryOptions {
  limit?: number;
  order?: 'asc' | 'desc';
  event_type?: 'anomaly' | 'recovery';
  subsystem?: Subsystem;
  severity?: Severity;
  /** Only meaningful combined with `event_type: 'recovery'`, or with no
   * `event_type` at all — the backend rejects `event_type: 'anomaly'` +
   * `status` together with `400 INVALID_STATUS` (anomaly events have no
   * status field). */
  status?: RecoveryStatus;
}

/** GET /metrics -> one metric's unit (Step 43). Matches
 * `shared/metrics.py`'s `Unit` class exactly — there is no third unit. */
export type MetricUnit = 'Count' | 'Milliseconds';

/** GET /metrics -> one item of a series's "datapoints" array. `value` is
 * a CloudWatch per-period statistic (`Sum` for Count metrics, `Average`
 * for Milliseconds metrics — see `MetricSeries`'s own doc comment), NEVER
 * a raw per-event observation and NEVER a fabricated/zero-filled value:
 * a period with no published data simply has no entry here. */
export interface MetricDatapoint {
  timestamp: string;
  value: number;
}

/** GET /metrics -> one series's optional lightweight aggregate over the
 * WHOLE returned window (never a percentile — this project's data does
 * not support one). Only present when the series has at least one
 * datapoint. `sum` for Count-unit metrics, `average` for
 * Milliseconds-unit metrics — never both, and an `average` must always
 * be presented as "Average," never as "MTTR" (see this project's own
 * documented RecoveryLatency semantics: combined detect+remediate time,
 * not isolated remediation duration). */
export interface MetricSeriesSummary {
  datapoint_count: number;
  sum?: number;
  average?: number;
}

/** GET /metrics -> one item of the "series" array (Step 43). One series
 * is exactly one (metric_name, Subsystem, FaultType) combination — the
 * backend never returns a series for a dimension pair or metric name
 * outside the approved SentinelGrid contract (`shared/metrics.py`'s own
 * `MetricName`/`Unit`/dimension set). `unit`/`dimensions` are carried
 * through from CloudWatch unchanged, never relabeled.
 *
 * **`datapoints` may legitimately be `[]`.** A caller-specified, fully
 * bounded (`Subsystem` AND `FaultType` both given) request returns all 7
 * approved metrics even when a given one has no data in the window — an
 * honest "no datapoint," never a fabricated zero. See
 * `lambda_functions/knowledge_query/metrics_service.py`'s own docstring
 * for the exact empty-series rule (including when an empty series is
 * instead simply omitted from the response, for an unfiltered/
 * partially-filtered "discover what's available" query).
 */
export interface MetricSeries {
  metric_name: string;
  unit: MetricUnit;
  dimensions: { Subsystem: Subsystem; FaultType: FaultType };
  datapoints: MetricDatapoint[];
  summary?: MetricSeriesSummary;
}

/** GET /metrics -> 200 body (Step 43 — SRS FR-7.1/FR-7.2's read path for
 * the CloudWatch metrics `shared/metrics.py` already publishes).
 *
 * IMPORTANT semantics — read before assuming more than this says:
 *   - `start`/`end` are the ACTUAL resolved window (never absent, even
 *     when the request omitted one or both — the backend fills in the
 *     default-1-hour-ending-now window and echoes back what it actually
 *     used).
 *   - `period_seconds` is the CloudWatch statistical period actually
 *     used for every series in this response (60s for windows <= 1
 *     hour, larger for longer windows purely to bound how many
 *     datapoints one response returns — NEVER claiming finer than ~60s
 *     resolution, since that is the underlying publish rate; see
 *     `metrics_service.py`'s `_period_for_window` docstring).
 *   - This is observational CloudWatch data, not a computed SLA/accuracy
 *     figure — there is no detection-accuracy or false-positive-rate
 *     field here, and there will not be one (this project has no
 *     labeled ground-truth dataset to compute either from).
 */
export interface MetricsResponse {
  namespace: string;
  start: string;
  end: string;
  period_seconds: number;
  series: MetricSeries[];
  subsystem?: Subsystem;
  fault_type?: FaultType;
}

export interface GetMetricsQueryOptions {
  /** ISO-8601. Both optional — see `MetricsResponse`'s own doc comment
   * for what the backend defaults to when omitted. */
  start?: string;
  end?: string;
  subsystem?: Subsystem;
  fault_type?: FaultType;
}
