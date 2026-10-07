import { Link } from 'react-router-dom';
import { cn, humanize } from '@/lib/utils';
import type { StatusTone, Subsystem } from '@/types/domain';
import { StatusBadge } from './StatusBadge';

interface SubsystemCardProps {
  subsystem: Subsystem;
  nodeCount: number;
  healthyCount: number;
  tone: StatusTone;
  summary: string;
  icon: React.ReactNode;
}

/** One of the three subsystem overview cards (Power / Water / Traffic)
 * on the dashboard — links through to the Nodes page pre-filtered to
 * this subsystem. */
export function SubsystemCard({ subsystem, nodeCount, healthyCount, tone, summary, icon }: SubsystemCardProps) {
  const label = humanize(subsystem);

  return (
    <Link
      to={`/nodes?subsystem=${subsystem}`}
      className={cn(
        'group flex flex-col gap-3 rounded-xl border border-surface-border bg-surface-850 p-5',
        'transition-colors hover:border-brand-500/50 hover:bg-surface-800',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400',
      )}
      aria-label={`View ${label} nodes`}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-surface-700 text-brand-400">{icon}</span>
          <div>
            <h3 className="text-sm font-semibold text-slate-100">{label}</h3>
            <p className="text-xs text-slate-500">{nodeCount} nodes</p>
          </div>
        </div>
        <StatusBadge tone={tone} label={`${healthyCount}/${nodeCount} healthy`} />
      </div>
      <p className="text-sm text-slate-400">{summary}</p>
      <span className="mt-auto text-xs font-medium text-brand-400 group-hover:text-brand-400/80">View {label} nodes &rarr;</span>
    </Link>
  );
}
