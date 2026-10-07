import { useMemo, useState } from 'react';
import { MetricChartCard, type MetricChartTone } from '@/components/analytics/MetricChartCard';
import { DataSourceTag } from '@/components/common/DataSourceTag';
import { EmptyState } from '@/components/common/EmptyState';
import { ErrorState } from '@/components/common/ErrorState';
import { LoadingState } from '@/components/common/LoadingState';
import { PageHeader } from '@/components/common/PageHeader';
import { Timestamp } from '@/components/common/Timestamp';
import { METRIC_DEFINITIONS } from '@/data/metricDefinitions';
import { useMetrics } from '@/hooks/useMetrics';
import { aggregateMetricSeries, resolveTimeRange, TIME_RANGE_OPTIONS, type TimeRangeOption } from '@/lib/analytics';
import { humanize } from '@/lib/utils';
import { FAULT_TYPES, SUBSYSTEMS, type FaultType, type Subsystem } from '@/types/domain';
import type { GetMetricsQueryOptions } from '@/types/api';

const METRIC_TONE: Partial<Record<string, MetricChartTone>> = {
  RecoverySuccessCount: 'healthy',
  RecoveryFailureCount: 'critical',
};

export function AnalyticsPage() {
  const [timeRange, setTimeRange] = useState<TimeRangeOption>('1h');
  const [subsystem, setSubsystem] = useState<Subsystem | 'all'>('all');
  const [faultType, setFaultType] = useState<FaultType | 'all'>('all');
  // Bumping this forces resolveTimeRange to re-anchor to "now" even when
  // no filter changed -- this IS the manual Refresh button's effect (no
  // automatic polling anywhere on this page).
  const [refreshNonce, setRefreshNonce] = useState(0);

  const queryOptions: GetMetricsQueryOptions = useMemo(() => {
    const { start, end } = resolveTimeRange(timeRange, new Date());
    return {
      start,
      end,
      subsystem: subsystem === 'all' ? undefined : subsystem,
      fault_type: faultType === 'all' ? undefined : faultType,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refreshNonce intentionally re-anchors "now" without adding a real dependency value
  }, [timeRange, subsystem, faultType, refreshNonce]);

  const { metrics, loading, error, loaded } = useMetrics(queryOptions);

  const handleRefresh = () => setRefreshNonce((n) => n + 1);

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Analytics"
        description="Detection and recovery metrics published by the backend to the SentinelGrid CloudWatch namespace — a bounded, recent window, not unlimited historical storage."
        actions={
          loaded ? <DataSourceTag isMock={false} /> : error ? <DataSourceTag isMock variant="pending" reason={error.message} /> : null
        }
      />

      <div className="rounded-xl border border-surface-border bg-surface-850 p-4 text-xs text-slate-400">
        These are CloudWatch <span className="font-mono text-slate-300">SentinelGrid</span> namespace observational
        measurements, not a computed SLA/accuracy figure. In particular:{' '}
        <span className="font-mono text-slate-300">RecoveryLatency</span> measures detection-to-recovery-completion time
        (combined detect+remediate), never isolated remediation duration or "MTTR"; and{' '}
        <span className="font-mono text-slate-300">UnresolvedFaultCount</span> is a raw escalation-observation count, not a
        continuously-maintained active-fault gauge. No detection-accuracy or false-positive-rate figure is shown — this
        project has no labeled ground-truth dataset to compute either from. See{' '}
        <span className="font-mono text-slate-300">docs/performance-validation.md</span> for controlled-test baselines.
      </div>

      <div className="flex flex-col gap-3 rounded-xl border border-surface-border bg-surface-850 p-4 sm:flex-row sm:flex-wrap sm:items-center">
        <label className="flex items-center gap-2 text-xs text-slate-400">
          Time range
          <select
            value={timeRange}
            onChange={(e) => setTimeRange(e.target.value as TimeRangeOption)}
            className="rounded-lg border border-surface-border bg-surface-900 px-2.5 py-1.5 text-sm text-slate-200 focus:border-brand-500 focus:outline-none"
          >
            {TIME_RANGE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-2 text-xs text-slate-400">
          Subsystem
          <select
            value={subsystem}
            onChange={(e) => setSubsystem(e.target.value as Subsystem | 'all')}
            className="rounded-lg border border-surface-border bg-surface-900 px-2.5 py-1.5 text-sm text-slate-200 focus:border-brand-500 focus:outline-none"
          >
            <option value="all">All</option>
            {SUBSYSTEMS.map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-2 text-xs text-slate-400">
          Fault type
          <select
            value={faultType}
            onChange={(e) => setFaultType(e.target.value as FaultType | 'all')}
            className="rounded-lg border border-surface-border bg-surface-900 px-2.5 py-1.5 text-sm text-slate-200 focus:border-brand-500 focus:outline-none"
          >
            <option value="all">All</option>
            {FAULT_TYPES.map((f) => (
              <option key={f} value={f}>
                {humanize(f)}
              </option>
            ))}
          </select>
        </label>

        <button
          type="button"
          onClick={handleRefresh}
          disabled={loading}
          className="ml-auto rounded-lg border border-surface-border bg-surface-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-surface-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Refresh
        </button>
      </div>

      {loading ? (
        <LoadingState label="Loading metrics…" rows={7} />
      ) : error ? (
        <ErrorState
          title="Could not load metrics"
          message={`${error.message} If this is a CORS error, confirm the API's FrontendOrigin matches this page's origin — see frontend/README.md.`}
          onRetry={handleRefresh}
        />
      ) : !metrics || metrics.series.length === 0 ? (
        <EmptyState
          title="No metric datapoints found"
          description="No SentinelGrid CloudWatch metrics were published in the selected window/filters. Try a wider time range or a different subsystem/fault type."
        />
      ) : (
        <>
          <p className="text-xs text-slate-500">
            Window: <Timestamp iso={metrics.start} /> – <Timestamp iso={metrics.end} /> · period: {metrics.period_seconds}s
            {metrics.period_seconds > 60 ? ' (aggregated — the underlying metrics publish at ~60s resolution)' : ''}
          </p>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {METRIC_DEFINITIONS.map((metric) => (
              <MetricChartCard
                key={metric.name}
                metric={metric}
                aggregated={aggregateMetricSeries(metrics.series, metric.name, metric.unit)}
                tone={METRIC_TONE[metric.name]}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
