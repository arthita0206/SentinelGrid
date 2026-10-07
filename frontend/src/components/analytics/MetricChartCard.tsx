import type { AggregatedMetric } from '@/lib/analytics';
import type { MetricDefinition } from '@/data/metricDefinitions';

export type MetricChartTone = 'brand' | 'healthy' | 'critical';

interface MetricChartCardProps {
  metric: MetricDefinition;
  /** The live, aggregated series for this metric (Step 43) —
   * `undefined` only while the page itself is still loading (the page
   * shows one shared `LoadingState` rather than a per-card one; see
   * `AnalyticsPage`). Once loaded, always present, even with zero
   * datapoints (an honest empty chart, never a fabricated one). */
  aggregated: AggregatedMetric | undefined;
  /** Bar color — 'healthy'/'critical' for RecoverySuccessCount/
   * RecoveryFailureCount specifically (Step 43's explicit "make the
   * distinction visually obvious" requirement), 'brand' otherwise. */
  tone?: MetricChartTone;
}

const TONE_BAR_CLASS: Record<MetricChartTone, string> = {
  brand: 'bg-brand-500/60',
  healthy: 'bg-status-healthy/70',
  critical: 'bg-status-critical/70',
};

function formatSummary(aggregated: AggregatedMetric): string {
  if (!aggregated.summary) return '';
  if (aggregated.summary.kind === 'sum') {
    return `Sum: ${aggregated.summary.value}`;
  }
  // Milliseconds average -- explicitly labeled "Average," never "MTTR"
  // and never presented as a percentile (see lib/analytics.ts's own
  // docstring for why only sum/average are ever computed here).
  return `Average: ${Math.round(aggregated.summary.value)} ms`;
}

/** A small, dependency-free SVG-free bar chart — deliberately not
 * pulling in a full charting library, continuing Step 39's original UI
 * foundation choice. As of Step 43, renders the LIVE, CloudWatch-derived
 * `aggregated` series passed in — never a fabricated/synthetic one.
 * Shows an explicit, honest empty state when the loaded series has zero
 * datapoints, rather than a misleading flat/zero chart. */
export function MetricChartCard({ metric, aggregated, tone = 'brand' }: MetricChartCardProps) {
  const datapoints = aggregated?.datapoints ?? [];
  const hasData = datapoints.length > 0;
  const max = hasData ? Math.max(...datapoints.map((dp) => dp.value), 1) : 1;

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-surface-border bg-surface-850 p-4" data-testid={`metric-card-${metric.name}`}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-slate-100">{metric.name}</p>
          <p className="mt-0.5 text-xs text-slate-500">{metric.description}</p>
        </div>
        {metric.srsRef ? (
          <span className="shrink-0 rounded-full border border-surface-border px-2 py-0.5 text-[10px] font-semibold text-slate-500">
            {metric.srsRef}
          </span>
        ) : null}
      </div>

      {hasData ? (
        <>
          <div className="flex h-20 items-end gap-1" role="img" aria-label={`${metric.name} live trend`}>
            {datapoints.map((dp) => (
              <div
                key={dp.timestamp}
                className={`flex-1 rounded-t ${TONE_BAR_CLASS[tone]}`}
                style={{ height: `${Math.max(4, (dp.value / max) * 100)}%` }}
                title={`${dp.timestamp}: ${dp.value}`}
              />
            ))}
          </div>
          <div className="flex items-center justify-between text-[11px] text-slate-600">
            <span>Unit: {metric.unit}</span>
            <span>{formatSummary(aggregated!)}</span>
          </div>
          {aggregated && aggregated.seriesCount > 1 ? (
            <p className="text-[10px] text-slate-600">Aggregated across {aggregated.seriesCount} Subsystem/FaultType series.</p>
          ) : null}
        </>
      ) : (
        <div className="flex h-20 flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-surface-border text-center">
          <p className="text-xs text-slate-500">No datapoints published in the selected window.</p>
        </div>
      )}
    </div>
  );
}
