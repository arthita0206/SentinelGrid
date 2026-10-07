/**
 * Typed client for the deployed SentinelGridKnowledgeQuery API — see
 * lambda_functions/knowledge_query/README.md for the authoritative route
 * documentation this mirrors.
 *
 * This is the ONLY module in the frontend that knows the API's URL
 * shape/query parameters. UI code never builds a URL or calls `fetch`
 * directly — it goes through the typed functions below (or the hooks in
 * src/hooks/, which wrap these with mock-data fallback — see that
 * directory's own docs).
 *
 * No credentials of any kind are read, stored, or sent by this module —
 * the Knowledge Query API is unauthenticated by design (see the backend
 * README's own "Security / authentication limitation" section); this
 * client simply performs plain, unauthenticated GET requests.
 *
 * CORS (Step 40 update): the deployed API now has CORS enabled — see
 * infrastructure/knowledge-query.yaml's `KnowledgeQueryApi.CorsConfiguration`
 * — restricted to one explicit configurable origin (the `FrontendOrigin`
 * template parameter, defaulting to `http://localhost:5173`), not a
 * wildcard. A browser call from that exact origin can now succeed; a
 * call from any OTHER origin is still blocked by the browser, same as
 * before. Every function below makes a genuine network request — this
 * is real, working client code, not a stub — callers (see src/hooks/)
 * are still expected to handle a failure (network error, an origin the
 * API doesn't allow, or a genuine backend error) rather than assume
 * success.
 */

import type {
  AnomalyHistoryResponse,
  ApiErrorBody,
  EventListResponse,
  GetMetricsQueryOptions,
  HistoryQueryOptions,
  ListEventsQueryOptions,
  ListNodesQueryOptions,
  MetricsResponse,
  NodeCurrentStateResponse,
  NodeListResponse,
  NodeSummaryResponse,
  RecoveryHistoryResponse,
} from '@/types/api';

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.error;
  }
}

/** Raised when VITE_API_BASE_URL isn't configured — distinct from a
 * network/CORS failure so callers/tests can tell "not configured" apart
 * from "configured but unreachable". */
export class ApiNotConfiguredError extends Error {
  constructor() {
    super('VITE_API_BASE_URL is not configured.');
    this.name = 'ApiNotConfiguredError';
  }
}

function buildQueryString(options?: HistoryQueryOptions): string {
  if (!options) return '';
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set('limit', String(options.limit));
  if (options.order !== undefined) params.set('order', options.order);
  const query = params.toString();
  return query ? `?${query}` : '';
}

async function request<T>(path: string): Promise<T> {
  if (!API_BASE_URL) {
    throw new ApiNotConfiguredError();
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });

  const body = await response.json();

  if (!response.ok) {
    throw new ApiError(response.status, body as ApiErrorBody);
  }

  return body as T;
}

/** GET /nodes — fleet listing (Step 40). See `NodeListResponse`'s own
 * doc comment (src/types/api.ts) for exactly what `order` and a filtered
 * `count` do and don't guarantee — this function does not reinterpret
 * or paper over those semantics. */
export function getNodes(options?: ListNodesQueryOptions): Promise<NodeListResponse> {
  const params = new URLSearchParams();
  if (options?.limit !== undefined) params.set('limit', String(options.limit));
  if (options?.order !== undefined) params.set('order', options.order);
  if (options?.subsystem !== undefined) params.set('subsystem', options.subsystem);
  if (options?.state !== undefined) params.set('state', options.state);
  const query = params.toString();
  return request<NodeListResponse>(`/nodes${query ? `?${query}` : ''}`);
}

/** GET /events — fleet-wide event feed (Step 42). See `EventListResponse`'s
 * own doc comment (src/types/api.ts) for exactly what this does and does
 * not guarantee — a bounded, recent-ish feed via DynamoDB Scan on both
 * event tables, NOT a complete historical export. */
export function getEvents(options?: ListEventsQueryOptions): Promise<EventListResponse> {
  const params = new URLSearchParams();
  if (options?.limit !== undefined) params.set('limit', String(options.limit));
  if (options?.order !== undefined) params.set('order', options.order);
  if (options?.event_type !== undefined) params.set('event_type', options.event_type);
  if (options?.subsystem !== undefined) params.set('subsystem', options.subsystem);
  if (options?.severity !== undefined) params.set('severity', options.severity);
  if (options?.status !== undefined) params.set('status', options.status);
  const query = params.toString();
  return request<EventListResponse>(`/events${query ? `?${query}` : ''}`);
}

/** GET /metrics — CloudWatch metrics feed (Step 43). See
 * `MetricsResponse`'s own doc comment (src/types/api.ts) for the exact
 * bounded-window/empty-series semantics this function does not
 * reinterpret. `start`/`end` (ISO-8601), `subsystem`, `fault_type` are
 * the only accepted parameters — never an arbitrary metric name or
 * namespace, matching the backend's own closed allowlist. */
export function getMetrics(options?: GetMetricsQueryOptions): Promise<MetricsResponse> {
  const params = new URLSearchParams();
  if (options?.start !== undefined) params.set('start', options.start);
  if (options?.end !== undefined) params.set('end', options.end);
  if (options?.subsystem !== undefined) params.set('subsystem', options.subsystem);
  if (options?.fault_type !== undefined) params.set('fault_type', options.fault_type);
  const query = params.toString();
  return request<MetricsResponse>(`/metrics${query ? `?${query}` : ''}`);
}

/** GET /nodes/{node_id} — current state only. */
export function getNode(nodeId: string): Promise<NodeCurrentStateResponse> {
  return request<NodeCurrentStateResponse>(`/nodes/${encodeURIComponent(nodeId)}`);
}

/** GET /nodes/{node_id}/anomalies */
export function getNodeAnomalies(nodeId: string, options?: HistoryQueryOptions): Promise<AnomalyHistoryResponse> {
  return request<AnomalyHistoryResponse>(`/nodes/${encodeURIComponent(nodeId)}/anomalies${buildQueryString(options)}`);
}

/** GET /nodes/{node_id}/recoveries */
export function getNodeRecoveries(nodeId: string, options?: HistoryQueryOptions): Promise<RecoveryHistoryResponse> {
  return request<RecoveryHistoryResponse>(`/nodes/${encodeURIComponent(nodeId)}/recoveries${buildQueryString(options)}`);
}

/** GET /nodes/{node_id}/summary — current state + both histories combined. */
export function getNodeSummary(nodeId: string, options?: HistoryQueryOptions): Promise<NodeSummaryResponse> {
  return request<NodeSummaryResponse>(`/nodes/${encodeURIComponent(nodeId)}/summary${buildQueryString(options)}`);
}

/** Whether a base URL is configured at all — used by hooks/UI to decide
 * whether attempting a live call is even worth it, vs. going straight to
 * demo data with a clear "API not configured" reason. */
export function isApiConfigured(): boolean {
  return Boolean(API_BASE_URL);
}
