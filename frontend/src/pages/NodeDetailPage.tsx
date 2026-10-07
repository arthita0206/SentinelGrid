import { Link, useParams } from 'react-router-dom';
import { DataSourceTag } from '@/components/common/DataSourceTag';
import { EmptyState } from '@/components/common/EmptyState';
import { ErrorState } from '@/components/common/ErrorState';
import { LoadingState } from '@/components/common/LoadingState';
import { NodeStateBadge } from '@/components/common/NodeStateBadge';
import { PageHeader } from '@/components/common/PageHeader';
import { SeverityBadge } from '@/components/common/SeverityBadge';
import { Timestamp } from '@/components/common/Timestamp';
import { useNodeSummary } from '@/hooks/useNodeSummary';
import { humanize } from '@/lib/utils';
import type { AnomalyEvent, RecoveryEvent } from '@/types/api';

const BACK_TO_NODES = (
  <Link to="/nodes" className="text-brand-400 hover:text-brand-400/80">
    &larr; Back to Nodes
  </Link>
);

function IdentityField({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs tracking-wide text-slate-500 uppercase">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-200">{value}</dd>
    </div>
  );
}

function AnomalyCard({ event }: { event: AnomalyEvent }) {
  return (
    <li className="rounded-lg border border-surface-border bg-surface-900 p-3">
      <div className="flex items-center justify-between gap-2">
        <SeverityBadge severity={event.severity} />
        <span className="text-xs text-slate-500">
          <Timestamp iso={event.timestamp} />
        </span>
      </div>
      <p className="mt-2 text-sm font-medium text-slate-200">{humanize(event.fault_type)}</p>
      <p className="mt-1 text-xs text-slate-500">{event.reason}</p>
    </li>
  );
}

function RecoveryCard({ event }: { event: RecoveryEvent }) {
  return (
    <li className="rounded-lg border border-surface-border bg-surface-900 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-slate-300">{humanize(event.action)}</span>
        <span className="text-xs text-slate-500">
          <Timestamp iso={event.recorded_at} />
        </span>
      </div>
      <p className="mt-2 text-sm font-medium text-slate-200">{humanize(event.status)}</p>
      <p className="mt-1 text-xs text-slate-500">{event.reason}</p>
      <p className="mt-1 text-[11px] text-slate-600">{event.automatic ? 'Automatic remediation' : 'Manual / escalated'}</p>
    </li>
  );
}

export function NodeDetailPage() {
  const { nodeId = '' } = useParams<{ nodeId: string }>();
  const { summary, loading, error, notFound, loaded, refetch } = useNodeSummary(nodeId);

  if (loading) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader title={nodeId} description={BACK_TO_NODES} />
        <LoadingState label="Loading node summary…" rows={6} />
      </div>
    );
  }

  // "Node not found" (a real 404 for a node that genuinely doesn't
  // exist) is deliberately its own distinct state -- never presented the
  // same way as a network/API failure, and never papered over with
  // fabricated data (see useNodeSummary's own docstring, Step 44).
  if (notFound) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader title={nodeId} description={BACK_TO_NODES} actions={<DataSourceTag isMock variant="pending" reason="No current state exists for this node." />} />
        <EmptyState
          title="Node not found"
          description={`No current state exists for node "${nodeId}" in SentinelGridNodeState. Check the node ID, or return to the fleet list.`}
          action={
            <Link
              to="/nodes"
              className="rounded-lg border border-surface-border bg-surface-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-surface-700"
            >
              Back to Nodes
            </Link>
          }
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader title={nodeId} description={BACK_TO_NODES} actions={<DataSourceTag isMock variant="pending" reason={error.message} />} />
        <ErrorState
          title="Could not load this node"
          message={`${error.message} This is an API error, not confirmation that the node has no data — retry, or check the API's CORS/FrontendOrigin configuration (see frontend/README.md).`}
          onRetry={refetch}
        />
      </div>
    );
  }

  if (!loaded || !summary) {
    // Defensive only -- loading/notFound/error above are exhaustive for
    // every way useNodeSummary's state can resolve; this should be
    // unreachable in practice.
    return null;
  }

  const { current_state: state, anomalies, recoveries } = summary;

  return (
    <div className="flex flex-col gap-8">
      <PageHeader title={state.node_id} description={BACK_TO_NODES} actions={<DataSourceTag isMock={false} />} />

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-xl border border-surface-border bg-surface-850 p-5 lg:col-span-2">
          <h2 className="mb-4 text-sm font-semibold text-slate-200">Node Identity &amp; Current State</h2>
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
            <IdentityField label="Node ID" value={<span className="font-mono">{state.node_id}</span>} />
            <IdentityField label="Subsystem" value={humanize(state.subsystem)} />
            <IdentityField label="Status" value={state.status} />
            <IdentityField label="Current State" value={<NodeStateBadge state={state.state} />} />
            <IdentityField label="Last Telemetry" value={<Timestamp iso={state.last_telemetry_timestamp} />} />
            <IdentityField label="Last State Update" value={<Timestamp iso={state.last_state_update} />} />
          </dl>
          <div className="mt-4 border-t border-surface-border pt-4">
            <dt className="text-xs tracking-wide text-slate-500 uppercase">Reason</dt>
            <dd className="mt-1 text-sm text-slate-300">{state.reason}</dd>
          </div>
        </div>

        <div className="rounded-xl border border-surface-border bg-surface-850 p-5">
          <h2 className="mb-4 text-sm font-semibold text-slate-200">Current Fault</h2>
          {state.fault_type ? (
            <div className="flex flex-col gap-2">
              <p className="text-sm font-medium text-slate-200">{humanize(state.fault_type)}</p>
              <SeverityBadge severity={state.fault_severity} />
            </div>
          ) : (
            <EmptyState title="No active fault" description="This node is not currently reporting a fault." />
          )}
        </div>
      </section>

      <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-200">Anomaly History</h2>
            <span className="text-xs text-slate-500">{anomalies.count} total</span>
          </div>
          {anomalies.items.length === 0 ? (
            <EmptyState title="No anomalies recorded" description="This node has no anomaly history in the queried window." />
          ) : (
            <ul className="flex flex-col gap-2">
              {anomalies.items.map((event) => (
                <AnomalyCard key={`${event.node_id}-${event.timestamp}`} event={event} />
              ))}
            </ul>
          )}
        </div>

        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-200">Recovery History</h2>
            <span className="text-xs text-slate-500">{recoveries.count} total</span>
          </div>
          {recoveries.items.length === 0 ? (
            <EmptyState title="No recovery events recorded" description="This node has no recovery history in the queried window." />
          ) : (
            <ul className="flex flex-col gap-2">
              {recoveries.items.map((event) => (
                <RecoveryCard key={`${event.node_id}-${event.timestamp}`} event={event} />
              ))}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}
