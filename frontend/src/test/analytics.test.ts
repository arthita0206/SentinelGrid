import { describe, expect, it } from 'vitest';
import { aggregateMetricSeries, resolveTimeRange, TIME_RANGE_OPTIONS } from '@/lib/analytics';
import type { MetricSeries } from '@/types/api';

function series(overrides: Partial<MetricSeries> & { metric_name: string; datapoints: MetricSeries['datapoints'] }): MetricSeries {
  return {
    unit: 'Count',
    dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
    ...overrides,
  };
}

describe('resolveTimeRange', () => {
  const now = new Date('2026-06-01T12:00:00.000Z');

  it('15m resolves to a 900-second window ending now', () => {
    const { start, end } = resolveTimeRange('15m', now);
    expect(end).toBe(now.toISOString());
    expect(new Date(end).getTime() - new Date(start).getTime()).toBe(15 * 60 * 1000);
  });

  it('1h resolves to a 3600-second window', () => {
    const { start, end } = resolveTimeRange('1h', now);
    expect(new Date(end).getTime() - new Date(start).getTime()).toBe(60 * 60 * 1000);
  });

  it('6h resolves to a 21600-second window', () => {
    const { start, end } = resolveTimeRange('6h', now);
    expect(new Date(end).getTime() - new Date(start).getTime()).toBe(6 * 60 * 60 * 1000);
  });

  it('24h resolves to exactly the backend maximum, never more', () => {
    const { start, end } = resolveTimeRange('24h', now);
    expect(new Date(end).getTime() - new Date(start).getTime()).toBe(24 * 60 * 60 * 1000);
  });

  it('every option has a distinct, ascending window size', () => {
    const seconds = TIME_RANGE_OPTIONS.map((o) => o.seconds);
    expect(seconds).toEqual([...seconds].sort((a, b) => a - b));
    expect(new Set(seconds).size).toBe(seconds.length);
  });
});

describe('aggregateMetricSeries', () => {
  it('returns zero series/datapoints when nothing matches the metric name', () => {
    const result = aggregateMetricSeries([], 'FaultDetectionCount', 'Count');
    expect(result.seriesCount).toBe(0);
    expect(result.datapoints).toEqual([]);
    expect(result.summary).toBeUndefined();
  });

  it('passes through a single series unchanged (no aggregation needed)', () => {
    const s = series({
      metric_name: 'FaultDetectionCount',
      datapoints: [{ timestamp: '2026-01-01T00:00:00+00:00', value: 3 }],
    });
    const result = aggregateMetricSeries([s], 'FaultDetectionCount', 'Count');
    expect(result.seriesCount).toBe(1);
    expect(result.datapoints).toEqual([{ timestamp: '2026-01-01T00:00:00+00:00', value: 3 }]);
    expect(result.summary).toEqual({ kind: 'sum', value: 3 });
  });

  it('sums Count-unit values across multiple dimension combinations at the same timestamp', () => {
    const a = series({
      metric_name: 'FaultDetectionCount',
      dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
      datapoints: [{ timestamp: '2026-01-01T00:00:00+00:00', value: 2 }],
    });
    const b = series({
      metric_name: 'FaultDetectionCount',
      dimensions: { Subsystem: 'water', FaultType: 'SENSOR_DROPOUT' },
      datapoints: [{ timestamp: '2026-01-01T00:00:00+00:00', value: 5 }],
    });
    const result = aggregateMetricSeries([a, b], 'FaultDetectionCount', 'Count');
    expect(result.seriesCount).toBe(2);
    expect(result.datapoints).toEqual([{ timestamp: '2026-01-01T00:00:00+00:00', value: 7 }]);
    expect(result.summary).toEqual({ kind: 'sum', value: 7 });
  });

  it('averages Milliseconds-unit values across multiple dimension combinations at the same timestamp', () => {
    const a = series({
      metric_name: 'DetectionLatency',
      unit: 'Milliseconds',
      dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
      datapoints: [{ timestamp: '2026-01-01T00:00:00+00:00', value: 100 }],
    });
    const b = series({
      metric_name: 'DetectionLatency',
      unit: 'Milliseconds',
      dimensions: { Subsystem: 'water', FaultType: 'SENSOR_DROPOUT' },
      datapoints: [{ timestamp: '2026-01-01T00:00:00+00:00', value: 300 }],
    });
    const result = aggregateMetricSeries([a, b], 'DetectionLatency', 'Milliseconds');
    expect(result.datapoints).toEqual([{ timestamp: '2026-01-01T00:00:00+00:00', value: 200 }]);
    expect(result.summary).toEqual({ kind: 'average', value: 200 });
  });

  it('never computes a percentile -- summary is always sum xor average', () => {
    const s = series({
      metric_name: 'RecoveryLatency',
      unit: 'Milliseconds',
      datapoints: [
        { timestamp: '2026-01-01T00:00:00+00:00', value: 100 },
        { timestamp: '2026-01-01T00:01:00+00:00', value: 300 },
      ],
    });
    const result = aggregateMetricSeries([s], 'RecoveryLatency', 'Milliseconds');
    expect(result.summary?.kind).toBe('average');
    expect(JSON.stringify(result)).not.toMatch(/p95|percentile|mttr/i);
  });

  it('ignores series belonging to a different metric name', () => {
    const wrongMetric = series({
      metric_name: 'RecoveryAttemptCount',
      datapoints: [{ timestamp: '2026-01-01T00:00:00+00:00', value: 99 }],
    });
    const result = aggregateMetricSeries([wrongMetric], 'FaultDetectionCount', 'Count');
    expect(result.seriesCount).toBe(0);
    expect(result.datapoints).toEqual([]);
  });

  it('sorts combined datapoints by timestamp ascending regardless of input order', () => {
    const s = series({
      metric_name: 'FaultDetectionCount',
      datapoints: [
        { timestamp: '2026-01-01T00:05:00+00:00', value: 1 },
        { timestamp: '2026-01-01T00:00:00+00:00', value: 2 },
      ],
    });
    const result = aggregateMetricSeries([s], 'FaultDetectionCount', 'Count');
    expect(result.datapoints.map((dp) => dp.timestamp)).toEqual(['2026-01-01T00:00:00+00:00', '2026-01-01T00:05:00+00:00']);
  });
});
