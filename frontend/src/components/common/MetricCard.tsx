import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';
import type { StatusTone } from '@/types/domain';
import { TONE_CLASSES } from '@/lib/status';

interface MetricCardProps {
  label: string;
  value: ReactNode;
  /** Small supporting line under the value, e.g. "of 60 total". */
  hint?: string;
  tone?: StatusTone;
  icon?: ReactNode;
  className?: string;
}

/** A compact stat card for summary rows (total nodes, healthy count,
 * active faults, ...). Purely presentational — callers decide whether
 * the value is live or demo data and surface that separately. */
export function MetricCard({ label, value, hint, tone = 'neutral', icon, className }: MetricCardProps) {
  const toneClasses = TONE_CLASSES[tone];

  return (
    <div
      className={cn(
        'rounded-xl border border-surface-border bg-surface-850 p-4 transition-colors hover:border-surface-600',
        className,
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-medium tracking-wide text-slate-400 uppercase">{label}</p>
        {icon ? <span className={cn('shrink-0', toneClasses.text)}>{icon}</span> : null}
      </div>
      <p className="mt-2 text-2xl font-semibold text-slate-50 tabular-nums">{value}</p>
      {hint ? <p className="mt-1 text-xs text-slate-500">{hint}</p> : null}
    </div>
  );
}
