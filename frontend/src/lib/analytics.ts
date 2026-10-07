import type { MetricSeries, MetricUnit } from '@/types/api';

/**
 * Pure, deterministic derivation helpers for the Analytics page (Step
 * 43). Each function takes the live `GET /metrics` result (or an empty
 * array) and computes UI-ready chart data — no fetching, no React, no
 * side effects, so these are fully unit-testable without mocking
 * `fetch`. `AnalyticsPage` is the only caller; keep it that way rather
 * than duplicating this logic inline in a component.
 */

// --- Time-range selector ---

export type TimeRangeOption = '15m' | '1h' | '6h' | '24h';

export const TIME_RANGE_OPTIONS: { value: TimeRangeOption; label: string; seconds: number }[] = [
  { value: '15m', label: 'Last 15 minutes', seconds: 15 * 60 },
  { value: '1h', label: 'Last 1 hour', seconds: 60 * 60 },
  { value: '6h', label: 'Last 6 hours', seconds: 6 * 60 * 60 },
  { value: '24h', label: 'Last 24 hours', seconds: 24 * 60 * 60 },
];

/** Resolves a `TimeRangeOption` into concrete ISO-8601 `start`/`end`
 * strings, anchored to `now` (injected for testability — production
 * callers pass `new Date()`). Never exceeds the backend's own 24-hour
 * maximum, since the largest option here (`'24h'`) is exactly that
 * ceiling, not a value that could exceed it.
 */
export function resolveTimeRange(option: TimeRangeOption, now: Date): { start: string; end: string } {
  const seconds = TIME_RANGE_OPTIONS.find((o) => o.value === option)?.seconds ?? 3600;
  const end = now;
  const start = new Date(end.getTime() - seconds * 1000);
  return { start: start.toISOString(), end: end.toISOString() };
}

// --- Per-metric aggregation across dimension combinations ---

export interface AggregatedDatapoint {
  timestamp: string;
  value: number;
}

export interface AggregatedMetricSummary {
  kind: 'sum' | 'average';
  value: number;
}

export interface AggregatedMetric {
  metricName: string;
  unit: MetricUnit;
  datapoints: AggregatedDatapoint[];
  /** How many distinct (Subsystem, FaultType) series contributed to
   * this aggregation — 0 means no live series for this metric name
   * exists in the current response (e.g. a subsystem/fault_type filter
   * combination CloudWatch has no data for), 1 means the current
   * subsystem+fault_type filters already narrowed to a single series
   * (no aggregation actually happened), >1 means multiple dimension
   * combinations were combined below. */
  seriesCount: number;
  summary?: AggregatedMetricSummary;
}

/**
 * Combines every `MetricSeries` in `series` whose `metric_name` matches
 * `metricName` into ONE chartable series, by summing (Count-unit
 * metrics) or averaging (Milliseconds-unit metrics) the values that
 * share the same `timestamp` across those series. This is necessary
 * because `GET /metrics` returns one series PER (metric, Subsystem,
 * FaultType) combination — when the Analytics page's own subsystem/
 * fault-type filters are not both set to one specific value, a single
 * metric can have up to 15 underlying series (see
 * `lambda_functions/knowledge_query/metrics_service.py`'s own dimension-
 * combination docstring), and one card per metric needs one combined
 * time series to plot.
 *
 * **Honest, not silently misleading**: `seriesCount` tells the caller
 * how many underlying series were combined, so the UI can disclose
 * "aggregated across N series" rather than presenting a combined number
 * as if it came from one dimension pair. Never computes a percentile —
 * only `sum` (Count) or `average` (Milliseconds), matching the backend's
 * own per-series `summary` semantics exactly (this function re-derives
 * the same aggregate ACROSS series the backend already computes WITHIN
 * one series).
 */
export function aggregateMetricSeries(series: MetricSeries[], metricName: string, unit: MetricUnit): AggregatedMetric {
  const matching = series.filter((s) => s.metric_name === metricName);

  const valuesByTimestamp = new Map<string, number[]>();
  for (const s of matching) {
    for (const dp of s.datapoints) {
      const existing = valuesByTimestamp.get(dp.timestamp);
      if (existing) {
        existing.push(dp.value);
      } else {
        valuesByTimestamp.set(dp.timestamp, [dp.value]);
      }
    }
  }

  const timestamps = [...valuesByTimestamp.keys()].sort();
  const datapoints: AggregatedDatapoint[] = timestamps.map((timestamp) => {
    const values = valuesByTimestamp.get(timestamp)!;
    const combined = unit === 'Count' ? values.reduce((a, b) => a + b, 0) : values.reduce((a, b) => a + b, 0) / values.length;
    return { timestamp, value: combined };
  });

  let summary: AggregatedMetricSummary | undefined;
  if (datapoints.length > 0) {
    const allValues = datapoints.map((dp) => dp.value);
    summary =
      unit === 'Count'
        ? { kind: 'sum', value: allValues.reduce((a, b) => a + b, 0) }
        : { kind: 'average', value: allValues.reduce((a, b) => a + b, 0) / allValues.length };
  }

  return { metricName, unit, datapoints, seriesCount: matching.length, summary };
}
