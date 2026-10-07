import { nodeStateTone } from '@/lib/status';
import { humanize } from '@/lib/utils';
import type { NodeState } from '@/types/domain';
import { StatusBadge } from './StatusBadge';

export function NodeStateBadge({ state, dotOnly }: { state: NodeState | string; dotOnly?: boolean }) {
  return <StatusBadge tone={nodeStateTone(state)} label={humanize(state)} dotOnly={dotOnly} />;
}
