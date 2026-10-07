/** Small, dependency-free class-name joiner (a minimal `clsx`-alike) —
 * filters out falsy values so conditional Tailwind classes stay readable
 * at call sites without pulling in an extra package for one helper. */
export function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(' ');
}

/** Formats an ISO-8601 timestamp for compact display, e.g. "14:32:08" for
 * today or "Sep 14, 14:32" for an earlier date. Returns the raw string
 * unchanged (never throws) if it doesn't parse — a malformed timestamp
 * should be visible, not hidden behind a crash. */
export function formatTimestamp(iso: string | undefined | null): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;

  const now = new Date();
  const isToday =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();

  if (isToday) {
    return date.toLocaleTimeString(undefined, { hour12: false });
  }
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

/** Title-cases a snake/upper-case backend token for display, e.g.
 * "NODE_OVERLOAD" -> "Node Overload", "power" -> "Power". */
export function humanize(token: string | undefined | null): string {
  if (!token) return '—';
  return token
    .toLowerCase()
    .split(/[_\s]+/)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}
