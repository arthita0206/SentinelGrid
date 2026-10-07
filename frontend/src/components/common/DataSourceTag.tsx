import { cn } from '@/lib/utils';

interface DataSourceTagProps {
  isMock: boolean;
  /** Only meaningful when `isMock` is true. Distinguishes two different
   * reasons a value isn't live (Step 41):
   *   - 'demo' (default) — a FABRICATED placeholder value is being
   *     shown in place of a real one (e.g. src/data/mock*.ts).
   *   - 'pending' — NO value (real or fake) is being shown at all; the
   *     section is an honest empty/placeholder state because the
   *     integration doesn't exist yet (e.g. CloudWatch metrics, event
   *     history). Never use 'demo' for a section that shows no
   *     fabricated numbers — that would (falsely) suggest the visible
   *     content is synthetic data, when actually there IS no content.
   */
  variant?: 'demo' | 'pending';
  /** Optional short reason shown as a tooltip when not live (e.g. "API
   * base URL not configured", or an error message) — helps distinguish
   * *why* a section isn't live. */
  reason?: string;
  className?: string;
}

/**
 * A small, unmissable indicator distinguishing demo/mock/pending data
 * from a live API response — required everywhere this app shows data
 * that could be anything other than live (Step 39: "clearly distinguish
 * mock/demo UI data from live data"; Step 41 added the 'pending'
 * variant for sections with no fabricated data at all, just not yet
 * connected). Never silently omitted: a component either shows this or
 * has no data-source ambiguity at all (e.g. static architecture text).
 */
export function DataSourceTag({ isMock, variant = 'demo', reason, className }: DataSourceTagProps) {
  const label = !isMock ? 'Live' : variant === 'pending' ? 'Not Connected' : 'Demo data';
  const tone = !isMock ? 'healthy' : variant === 'pending' ? 'neutral' : 'warning';

  const toneClasses: Record<'healthy' | 'warning' | 'neutral', { badge: string; dot: string }> = {
    healthy: { badge: 'border-status-healthy/30 bg-status-healthy-muted text-status-healthy', dot: 'bg-status-healthy' },
    warning: { badge: 'border-status-warning/30 bg-status-warning-muted text-status-warning', dot: 'bg-status-warning' },
    neutral: { badge: 'border-status-neutral/30 bg-status-neutral-muted text-status-neutral', dot: 'bg-status-neutral' },
  };

  return (
    <span
      title={reason}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold tracking-wide uppercase',
        toneClasses[tone].badge,
        className,
      )}
    >
      <span className={cn('h-1.5 w-1.5 rounded-full', toneClasses[tone].dot)} aria-hidden="true" />
      {label}
    </span>
  );
}
