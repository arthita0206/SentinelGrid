import type { NodeState, RecoveryStatus, Severity, StatusTone } from '@/types/domain';

/** Maps a backend NodeState onto one of the UI's five semantic tones. A
 * new NodeState value would fall through to 'neutral' rather than throw,
 * so an unrecognized future value degrades gracefully instead of
 * crashing the dashboard. */
export function nodeStateTone(state: NodeState | string | undefined | null): StatusTone {
  switch (state) {
    case 'HEALTHY':
    case 'RECOVERED':
      return 'healthy';
    case 'RECOVERING':
      return 'info';
    case 'FAULT_DETECTED':
      return 'warning';
    case 'ESCALATION_REQUIRED':
      return 'critical';
    default:
      return 'neutral';
  }
}

export function severityTone(severity: Severity | string | undefined | null): StatusTone {
  switch (severity) {
    case 'NORMAL':
    case 'LOW':
      return 'healthy';
    case 'MEDIUM':
      return 'warning';
    case 'HIGH':
    case 'CRITICAL':
      return 'critical';
    default:
      return 'neutral';
  }
}

export function recoveryStatusTone(status: RecoveryStatus | string | undefined | null): StatusTone {
  switch (status) {
    case 'SUCCESS':
      return 'healthy';
    case 'FAILURE':
      return 'critical';
    // 'critical', matching nodeStateTone's own ESCALATION_REQUIRED ->
    // 'critical' mapping (Step 44 consistency fix) -- the same status
    // value must carry the same severity color everywhere in the app,
    // not 'warning' here and 'critical' elsewhere for the identical
    // concept.
    case 'ESCALATION_REQUIRED':
      return 'critical';
    default:
      return 'neutral';
  }
}

export function telemetryStatusTone(status: string | undefined | null): StatusTone {
  return status === 'ONLINE' ? 'healthy' : status === 'OFFLINE' ? 'critical' : 'neutral';
}

/** Tailwind utility classes for each tone, centralized so every badge/
 * indicator in the app stays visually consistent. `dot` is the small
 * solid-color variant used for compact status dots. */
export const TONE_CLASSES: Record<StatusTone, { badge: string; dot: string; text: string }> = {
  healthy: {
    badge: 'bg-status-healthy-muted text-status-healthy border-status-healthy/30',
    dot: 'bg-status-healthy',
    text: 'text-status-healthy',
  },
  warning: {
    badge: 'bg-status-warning-muted text-status-warning border-status-warning/30',
    dot: 'bg-status-warning',
    text: 'text-status-warning',
  },
  critical: {
    badge: 'bg-status-critical-muted text-status-critical border-status-critical/30',
    dot: 'bg-status-critical',
    text: 'text-status-critical',
  },
  info: {
    badge: 'bg-status-info-muted text-status-info border-status-info/30',
    dot: 'bg-status-info',
    text: 'text-status-info',
  },
  neutral: {
    badge: 'bg-status-neutral-muted text-status-neutral border-status-neutral/30',
    dot: 'bg-status-neutral',
    text: 'text-status-neutral',
  },
};
