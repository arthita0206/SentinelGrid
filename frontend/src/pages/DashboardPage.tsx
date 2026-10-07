import { Link } from 'react-router-dom';
import { AlertIcon, CheckCircleIcon, ClockIcon, NodesIcon, PowerIcon, TrafficIcon, WaterIcon } from '@/components/icons';
import { DataSourceTag } from '@/components/common/DataSourceTag';
import { EmptyState } from '@/components/common/EmptyState';
import { ErrorState } from '@/components/common/ErrorState';
import { LoadingState } from '@/components/common/LoadingState';
import { MetricCard } from '@/components/common/MetricCard';
import { PageHeader } from '@/components/common/PageHeader';
import { StatusBadge } from '@/components/common/StatusBadge';
import { SubsystemCard } from '@/components/common/SubsystemCard';
import { CityVisualization } from '@/components/dashboard/CityVisualization';
import { useNodes } from '@/hooks/useNodes';
import { deriveDashboardStats, deriveSubsystemStats, deriveSystemStatus } from '@/lib/dashboard';
import type { Subsystem } from '@/types/domain';

const SUBSYSTEM_META: Record<Subsystem, { icon: React.ReactNode; summary: string }> = {
  power: { icon: <PowerIcon className="h-5 w-5" />, summary: 'Grid load, voltage, and frequency across all power nodes.' },
  water: { icon: <WaterIcon className="h-5 w-5" />, summary: 'Pressure, flow rate, and reservoir levels across the water network.' },
  traffic: { icon: <TrafficIcon className="h-5 w-5" />, summary: 'Vehicle counts, average speed, and signal state across intersections.' },
};

// One bounded request for the whole page -- matches SRS NFR-3's target
// fleet size and Step 40's own recommendation ("prefer one GET /nodes
// request with limit=500 rather than separate requests per subsystem").
// Subsystem/summary/system-status figures are all derived locally from
// this single response (see src/lib/dashboard.ts) -- never three
// separate API calls.
const FLEET_FETCH_LIMIT = 500;

function RefreshButton({ onClick, disabled }: { onClick: () => void; disabled: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-lg border border-surface-border bg-surface-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-surface-700 disabled:cursor-not-allowed disabled:opacity-50"
    >
      Refresh
    </button>
  );
}

export function DashboardPage() {
  // Manual refresh only (the hook's own `refetch`) -- Step 41 explicitly
  // rules out automatic polling; a request is made once on mount and
  // again only when the operator clicks Refresh or Retry.
  const { nodes, loading, error, loaded, refetch } = useNodes({ limit: FLEET_FETCH_LIMIT, order: 'asc' });

  const connectivityBadge = loading ? (
    <StatusBadge tone="neutral" label="Connecting to API…" />
  ) : error ? (
    <StatusBadge tone="critical" label="API Unavailable" />
  ) : (
    <StatusBadge tone="info" label="API Connected" />
  );

  // The system-status badge is a DASHBOARD-LEVEL INTERPRETATION of the
  // current node fleet (see deriveSystemStatus's own docstring for the
  // exact precedence rule) -- NOT an AWS/CloudWatch health check, and
  // deliberately separate from `connectivityBadge` above (a successful
  // API call and a healthy fleet are two different facts).
  const systemStatus = loaded ? deriveSystemStatus(nodes) : null;

  const stats = loaded ? deriveDashboardStats(nodes) : null;
  const subsystemStats = loaded ? deriveSubsystemStats(nodes) : null;

  return (
    <div className="flex flex-col gap-8">
      <PageHeader
        title="SentinelGrid"
        description="Smart City Resilience & Operations"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {connectivityBadge}
            {systemStatus ? <StatusBadge tone={systemStatus.tone} label={systemStatus.label} /> : null}
            {loaded ? <DataSourceTag isMock={false} /> : null}
            <RefreshButton onClick={refetch} disabled={loading} />
          </div>
        }
      />

      {loading ? (
        <LoadingState label="Loading node fleet…" rows={8} />
      ) : error ? (
        <ErrorState
          title="Could not load dashboard data"
          message={`${error.message} If this is a CORS error, confirm the API's FrontendOrigin matches this page's origin — see frontend/README.md.`}
          onRetry={refetch}
        />
      ) : nodes.length === 0 ? (
        <EmptyState
          title="No nodes are currently reporting state"
          description="GET /nodes returned zero records from SentinelGridNodeState. This is an honest empty result, not an error — publish telemetry through the simulator to populate it."
        />
      ) : (
        <>
          {/* Summary cards */}
          <section aria-label="Fleet summary" className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <MetricCard label="Total Nodes" value={stats!.totalNodes} hint="Returned by GET /nodes" tone="info" icon={<NodesIcon className="h-5 w-5" />} />
            <MetricCard
              label="Healthy Nodes"
              value={stats!.healthyNodes}
              hint={`${Math.round((stats!.healthyNodes / stats!.totalNodes) * 100)}% of fleet`}
              tone="healthy"
              icon={<CheckCircleIcon className="h-5 w-5" />}
            />
            <MetricCard
              label="Active Faults"
              value={stats!.activeFaults}
              hint="FAULT_DETECTED + RECOVERING + ESCALATION_REQUIRED"
              tone={stats!.activeFaults > 0 ? 'critical' : 'healthy'}
              icon={<AlertIcon className="h-5 w-5" />}
            />
            <MetricCard
              label="Recovered Nodes"
              value={stats!.recoveredNodes}
              hint="Currently in state RECOVERED (not a historical count)"
              tone="info"
              icon={<ClockIcon className="h-5 w-5" />}
            />
          </section>

          {/* Subsystem cards */}
          <section aria-label="Subsystems" className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            {subsystemStats!.map((s) => (
              <SubsystemCard
                key={s.subsystem}
                subsystem={s.subsystem}
                nodeCount={s.nodeCount}
                healthyCount={s.healthyCount}
                tone={s.tone}
                summary={SUBSYSTEM_META[s.subsystem].summary}
                icon={SUBSYSTEM_META[s.subsystem].icon}
              />
            ))}
          </section>

          {/* City visualization + resilience metrics */}
          <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <div className="flex flex-col gap-2 lg:col-span-2">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-200">Simulated City &amp; Node View</h2>
                <DataSourceTag isMock={false} reason="Positions are a stylized layout, not geographic — node identities/states are live." />
              </div>
              <CityVisualization nodes={nodes} />
            </div>

            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-200">System Activity &amp; Resilience</h2>
                <DataSourceTag isMock variant="pending" reason="Not shown on this page — GET /nodes (what this dashboard fetches) has no CloudWatch data. See the Analytics page for live metrics." />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <MetricCard label="Detection Latency" value="—" hint="See Analytics" tone="neutral" />
                <MetricCard label="Recovery Latency" value="—" hint="See Analytics" tone="neutral" />
                <MetricCard label="Recovery Success" value="—" hint="See Analytics" tone="neutral" />
                <MetricCard label="Unresolved Faults" value="—" hint="See Analytics" tone="neutral" />
              </div>
              <p className="text-xs text-slate-600">
                These are CloudWatch-derived metrics, distinct from the NodeState data this dashboard fetches — the{' '}
                <Link to="/analytics" className="text-brand-400 hover:text-brand-400/80">
                  Analytics page
                </Link>{' '}
                shows them live, from <span className="font-mono">GET /metrics</span>. See also{' '}
                <span className="font-mono">docs/performance-validation.md</span> for measured latency evidence from
                controlled backend tests.
              </p>
            </div>
          </section>

          {/* Recent events */}
          <section className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-200">Recent Events</h2>
              <DataSourceTag isMock variant="pending" reason="Not shown on this page — GET /nodes provides current state only, not historical anomaly/recovery events. See the Events page." />
            </div>
            <EmptyState
              title="Recent event history is not shown on this dashboard."
              description="GET /nodes (what this dashboard fetches) returns current node state only — it does not provide anomaly/recovery history, and querying per-node history for every visible node here would require an unbounded number of requests."
              action={
                <Link
                  to="/events"
                  className="rounded-lg border border-surface-border bg-surface-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-surface-700"
                >
                  Go to Events
                </Link>
              }
            />
          </section>
        </>
      )}
    </div>
  );
}
