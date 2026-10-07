import { Link } from 'react-router-dom';
import { humanize } from '@/lib/utils';
import { recoveryStatusTone, severityTone } from '@/lib/status';
import type { RecoveryStatus, Severity, Subsystem } from '@/types/domain';
import { EmptyState } from './EmptyState';
import { SeverityBadge } from './SeverityBadge';
import { StatusBadge } from './StatusBadge';
import { Timestamp } from './Timestamp';

/** A normalized row shape covering both anomaly and recovery events, so
 * the Events page (and the node detail page's history sections) can
 * render both kinds through one table component. Built from
 * AnomalyEvent/RecoveryEvent (or, as of Step 42, `FleetEvent`) by the
 * page that fetches them — this component itself performs no data
 * fetching or API-shape knowledge. */
export interface EventRow {
  id: string;
  kind: 'anomaly' | 'recovery' | 'escalation';
  timestamp: string;
  nodeId: string;
  subsystem: Subsystem;
  faultType: string;
  severity: Severity;
  action?: string;
  status?: RecoveryStatus | 'DETECTED';
  /** Recovery events only (Step 42) — whether the remediation action
   * itself succeeded. `undefined`/`null` for anomaly rows, where the
   * concept doesn't apply. */
  success?: boolean | null;
}

const KIND_LABEL: Record<EventRow['kind'], string> = {
  anomaly: 'Anomaly',
  recovery: 'Recovery',
  escalation: 'Escalation',
};

// Anomaly rows are fault/severity-oriented (an amber "something was
// detected" signal); recovery rows are action/status-oriented (green =
// the system responded); escalation rows (a recovery event whose status
// is ESCALATION_REQUIRED) are the most severe -- red. This makes the
// three kinds visually distinguishable at a glance, not just by the
// text label in the Type column.
const KIND_TONE: Record<EventRow['kind'], 'warning' | 'healthy' | 'critical'> = {
  anomaly: 'warning',
  recovery: 'healthy',
  escalation: 'critical',
};

export function EventTable({ events }: { events: EventRow[] }) {
  if (events.length === 0) {
    return <EmptyState title="No events match the current filters" description="Try adjusting the filters above, or widen the time range." />;
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-surface-border">
      <table className="w-full min-w-[920px] text-left text-sm">
        <thead>
          <tr className="border-b border-surface-border bg-surface-850 text-xs tracking-wide text-slate-400 uppercase">
            <th scope="col" className="px-4 py-3 font-medium">Timestamp</th>
            <th scope="col" className="px-4 py-3 font-medium">Node</th>
            <th scope="col" className="px-4 py-3 font-medium">Subsystem</th>
            <th scope="col" className="px-4 py-3 font-medium">Type</th>
            <th scope="col" className="px-4 py-3 font-medium">Fault Type</th>
            <th scope="col" className="px-4 py-3 font-medium">Severity</th>
            <th scope="col" className="px-4 py-3 font-medium">Action</th>
            <th scope="col" className="px-4 py-3 font-medium">Status</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-border">
          {events.map((event) => (
            <tr key={event.id} className="transition-colors hover:bg-surface-850">
              <td className="px-4 py-3 text-xs text-slate-500">
                <Timestamp iso={event.timestamp} />
              </td>
              <td className="px-4 py-3 font-mono text-xs text-slate-200">
                <Link to={`/nodes/${event.nodeId}`} className="hover:text-brand-400 focus-visible:text-brand-400">
                  {event.nodeId}
                </Link>
              </td>
              <td className="px-4 py-3 text-slate-300">{humanize(event.subsystem)}</td>
              <td className="px-4 py-3">
                <StatusBadge tone={KIND_TONE[event.kind]} label={KIND_LABEL[event.kind]} />
              </td>
              <td className="px-4 py-3 text-slate-400">{humanize(event.faultType)}</td>
              <td className="px-4 py-3">
                <SeverityBadge severity={event.severity} />
              </td>
              <td className="px-4 py-3 text-slate-400">{event.action && event.action !== 'NONE' ? humanize(event.action) : '—'}</td>
              <td className="px-4 py-3">
                <div className="flex items-center gap-1.5">
                  {event.status ? (
                    event.status === 'DETECTED' ? (
                      <StatusBadge tone={severityTone(event.severity)} label="Detected" />
                    ) : (
                      <StatusBadge tone={recoveryStatusTone(event.status)} label={humanize(event.status)} />
                    )
                  ) : (
                    '—'
                  )}
                  {event.success === true ? (
                    <span className="text-status-healthy" title="Succeeded" aria-label="Succeeded">
                      ✓
                    </span>
                  ) : event.success === false ? (
                    <span className="text-status-critical" title="Did not succeed" aria-label="Did not succeed">
                      ✗
                    </span>
                  ) : null}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
