import { severityTone } from '@/lib/status';
import { humanize } from '@/lib/utils';
import type { Severity } from '@/types/domain';
import { StatusBadge } from './StatusBadge';

export function SeverityBadge({ severity, dotOnly }: { severity: Severity | string | null | undefined; dotOnly?: boolean }) {
  if (!severity) return <StatusBadge tone="neutral" label="None" dotOnly={dotOnly} />;
  return <StatusBadge tone={severityTone(severity)} label={humanize(severity)} dotOnly={dotOnly} />;
}
