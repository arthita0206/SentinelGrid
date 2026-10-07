import { useMemo, useState } from 'react';
import { DataSourceTag } from '@/components/common/DataSourceTag';
import { EmptyState } from '@/components/common/EmptyState';
import { ErrorState } from '@/components/common/ErrorState';
import { EventTable, type EventRow } from '@/components/common/EventTable';
import { LoadingState } from '@/components/common/LoadingState';
import { PageHeader } from '@/components/common/PageHeader';
import { useEvents } from '@/hooks/useEvents';
import { humanize } from '@/lib/utils';
import { SUBSYSTEMS, type RecoveryStatus, type Severity, type Subsystem } from '@/types/domain';
import type { FleetEvent, ListEventsQueryOptions } from '@/types/api';

type EventTypeFilter = 'all' | 'anomaly' | 'recovery';
type StatusFilter = 'all' | RecoveryStatus;

// The event feed is bounded (max 100 per the backend's own MAX_EVENT_LIMIT)
// -- this is a recent/bounded operations feed, not a complete historical
// export. See frontend/README.md's "Events (Step 42)" section for the
// full "why bounded Scan, not a complete query" reasoning.
const EVENT_FETCH_LIMIT = 50;

function toEventRow(event: FleetEvent): EventRow {
  const kind: EventRow['kind'] =
    event.event_type === 'anomaly' ? 'anomaly' : event.status === 'ESCALATION_REQUIRED' ? 'escalation' : 'recovery';

  return {
    id: `${event.event_type}-${event.node_id}-${event.timestamp}`,
    kind,
    timestamp: event.timestamp,
    nodeId: event.node_id,
    subsystem: event.subsystem,
    faultType: event.fault_type,
    severity: event.severity,
    action: event.action ?? undefined,
    status: event.event_type === 'anomaly' ? 'DETECTED' : (event.status ?? undefined),
    success: event.success,
  };
}

export function EventsPage() {
  const [eventType, setEventType] = useState<EventTypeFilter>('all');
  const [subsystem, setSubsystem] = useState<Subsystem | 'all'>('all');
  const [severity, setSeverity] = useState<Severity | 'all'>('all');
  const [status, setStatus] = useState<StatusFilter>('all');

  // `status` only applies to recovery events -- the backend rejects
  // event_type=anomaly combined with status (400 INVALID_STATUS, see
  // lambda_functions/knowledge_query/handler.py). Rather than let that
  // request fail, selecting "Anomaly" clears any status filter here.
  function handleEventTypeChange(value: EventTypeFilter) {
    setEventType(value);
    if (value === 'anomaly') setStatus('all');
  }

  const queryOptions: ListEventsQueryOptions = useMemo(
    () => ({
      limit: EVENT_FETCH_LIMIT,
      order: 'desc',
      event_type: eventType === 'all' ? undefined : eventType,
      subsystem: subsystem === 'all' ? undefined : subsystem,
      severity: severity === 'all' ? undefined : severity,
      status: status === 'all' ? undefined : status,
    }),
    [eventType, subsystem, severity, status],
  );

  const { events, loading, error, loaded, refetch } = useEvents(queryOptions);
  const rows = useMemo(() => events.map(toEventRow), [events]);

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Events"
        description="A bounded, recent feed of anomaly and recovery events across the fleet — not the complete historical database."
        actions={
          loaded ? (
            <DataSourceTag isMock={false} />
          ) : error ? (
            // 'pending', not the default 'demo': the error state shows no
            // fabricated data at all, just an honest ErrorState below.
            <DataSourceTag isMock variant="pending" reason={error.message} />
          ) : null
        }
      />

      <div className="flex flex-col gap-3 rounded-xl border border-surface-border bg-surface-850 p-4 sm:flex-row sm:flex-wrap sm:items-center">
        <label className="flex items-center gap-2 text-xs text-slate-400">
          Event type
          <select
            value={eventType}
            onChange={(e) => handleEventTypeChange(e.target.value as EventTypeFilter)}
            className="rounded-lg border border-surface-border bg-surface-900 px-2.5 py-1.5 text-sm text-slate-200 focus:border-brand-500 focus:outline-none"
          >
            <option value="all">All</option>
            <option value="anomaly">Anomaly</option>
            <option value="recovery">Recovery</option>
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
          Severity
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value as Severity | 'all')}
            className="rounded-lg border border-surface-border bg-surface-900 px-2.5 py-1.5 text-sm text-slate-200 focus:border-brand-500 focus:outline-none"
          >
            <option value="all">All</option>
            {(['NORMAL', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'] as Severity[]).map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-2 text-xs text-slate-400">
          Status
          <select
            value={status}
            disabled={eventType === 'anomaly'}
            title={eventType === 'anomaly' ? 'Status only applies to recovery events.' : undefined}
            onChange={(e) => setStatus(e.target.value as StatusFilter)}
            className="rounded-lg border border-surface-border bg-surface-900 px-2.5 py-1.5 text-sm text-slate-200 focus:border-brand-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
          >
            <option value="all">All</option>
            <option value="SUCCESS">Success</option>
            <option value="FAILURE">Failure</option>
            <option value="ESCALATION_REQUIRED">Escalation Required</option>
          </select>
        </label>

        <button
          type="button"
          onClick={refetch}
          disabled={loading}
          className="ml-auto rounded-lg border border-surface-border bg-surface-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-surface-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Refresh
        </button>
      </div>

      {loading ? (
        <LoadingState label="Loading events…" rows={8} />
      ) : error ? (
        <ErrorState
          title="Could not load events"
          message={`${error.message} If this is a CORS error, confirm the API's FrontendOrigin matches this page's origin — see frontend/README.md.`}
          onRetry={refetch}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No anomaly or recovery events found."
          description="Either the fleet has no matching events right now, or the current filters exclude everything. This feed shows up to the most recent 50 events — not the complete historical database."
        />
      ) : (
        <>
          <p className="text-xs text-slate-500">Showing {rows.length} event(s) — a bounded, recent feed, not the complete historical database.</p>
          <EventTable events={rows} />
        </>
      )}
    </div>
  );
}
