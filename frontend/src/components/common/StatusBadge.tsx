import { TONE_CLASSES } from '@/lib/status';
import { cn } from '@/lib/utils';
import type { StatusTone } from '@/types/domain';

interface StatusBadgeProps {
  tone: StatusTone;
  label: string;
  /** Renders a small solid dot before the label — used for compact
   * inline placements (tables, lists) where a full pill badge would be
   * too heavy. */
  dotOnly?: boolean;
  className?: string;
}

/** The base semantic status indicator every other badge in the app is
 * built on (NodeStateBadge, SeverityBadge, ...) — centralizes the
 * healthy/warning/critical/info/neutral visual language in one place. */
export function StatusBadge({ tone, label, dotOnly = false, className }: StatusBadgeProps) {
  const classes = TONE_CLASSES[tone];

  if (dotOnly) {
    return (
      <span className={cn('inline-flex items-center gap-1.5 text-xs font-medium', classes.text, className)}>
        <span className={cn('h-1.5 w-1.5 rounded-full', classes.dot)} aria-hidden="true" />
        {label}
      </span>
    );
  }

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap',
        classes.badge,
        className,
      )}
    >
      <span className={cn('h-1.5 w-1.5 rounded-full', classes.dot)} aria-hidden="true" />
      {label}
    </span>
  );
}
