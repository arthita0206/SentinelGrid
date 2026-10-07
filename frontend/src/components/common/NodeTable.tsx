import { Link } from 'react-router-dom';
import { humanize } from '@/lib/utils';
import type { NodeCurrentState } from '@/types/api';
import { EmptyState } from './EmptyState';
import { NodeStateBadge } from './NodeStateBadge';
import { SeverityBadge } from './SeverityBadge';
import { Timestamp } from './Timestamp';

interface NodeTableProps {
  nodes: NodeCurrentState[];
}

/** The node fleet table used by the Nodes page. Filtering/sorting is the
 * caller's responsibility (NodesPage) — this component only renders
 * whatever list it's given, plus an empty state when it's empty. */
export function NodeTable({ nodes }: NodeTableProps) {
  if (nodes.length === 0) {
    return <EmptyState title="No nodes match the current filters" description="Try adjusting the subsystem, state, or search filters above." />;
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-surface-border">
      <table className="w-full min-w-[880px] text-left text-sm">
        <thead>
          <tr className="border-b border-surface-border bg-surface-850 text-xs tracking-wide text-slate-400 uppercase">
            <th scope="col" className="px-4 py-3 font-medium">Node ID</th>
            <th scope="col" className="px-4 py-3 font-medium">Subsystem</th>
            <th scope="col" className="px-4 py-3 font-medium">State</th>
            <th scope="col" className="px-4 py-3 font-medium">Fault Type</th>
            <th scope="col" className="px-4 py-3 font-medium">Severity</th>
            <th scope="col" className="px-4 py-3 font-medium">Last Telemetry</th>
            <th scope="col" className="px-4 py-3 font-medium">Last State Update</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-border">
          {nodes.map((node) => (
            <tr key={node.node_id} className="transition-colors hover:bg-surface-850">
              <td className="px-4 py-3 font-mono text-xs text-slate-200">
                <Link to={`/nodes/${node.node_id}`} className="hover:text-brand-400 focus-visible:text-brand-400">
                  {node.node_id}
                </Link>
              </td>
              <td className="px-4 py-3 text-slate-300">{humanize(node.subsystem)}</td>
              <td className="px-4 py-3">
                <NodeStateBadge state={node.state} />
              </td>
              <td className="px-4 py-3 text-slate-400">{node.fault_type ? humanize(node.fault_type) : '—'}</td>
              <td className="px-4 py-3">
                <SeverityBadge severity={node.fault_severity} />
              </td>
              <td className="px-4 py-3 text-xs text-slate-500">
                <Timestamp iso={node.last_telemetry_timestamp} />
              </td>
              <td className="px-4 py-3 text-xs text-slate-500">
                <Timestamp iso={node.last_state_update} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
