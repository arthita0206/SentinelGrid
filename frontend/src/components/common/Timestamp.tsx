import { formatTimestamp } from '@/lib/utils';

interface TimestampProps {
  iso: string | undefined | null;
  className?: string;
}

/** Renders a compact, human-readable timestamp (see `formatTimestamp` —
 * local time, no timezone abbreviation shown in the visible text) while
 * always keeping the exact original ISO-8601 value available: as the
 * semantic `<time dateTime>` attribute, and as a hover/long-press
 * `title` tooltip. This is the ONE place a formatted timestamp is
 * rendered, so every table/detail page that shows one gets the same
 * "exact value never lost" guarantee (Step 44) rather than each call
 * site needing to remember to add it. */
export function Timestamp({ iso, className }: TimestampProps) {
  if (!iso) return <span className={className}>—</span>;
  return (
    <time dateTime={iso} title={iso} className={className}>
      {formatTimestamp(iso)}
    </time>
  );
}
