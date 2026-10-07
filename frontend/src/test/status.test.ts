import { describe, expect, it } from 'vitest';
import { nodeStateTone, recoveryStatusTone, severityTone } from '@/lib/status';

describe('lib/status tone consistency (Step 44)', () => {
  it('ESCALATION_REQUIRED carries the same tone for both node state and recovery status', () => {
    // A real bug found during the Step 44 audit: the same concept
    // (escalation) was 'critical' in one place and 'warning' in
    // another. Both must now agree.
    expect(nodeStateTone('ESCALATION_REQUIRED')).toBe('critical');
    expect(recoveryStatusTone('ESCALATION_REQUIRED')).toBe('critical');
  });

  it('recoveryStatusTone: SUCCESS is healthy, FAILURE is critical', () => {
    expect(recoveryStatusTone('SUCCESS')).toBe('healthy');
    expect(recoveryStatusTone('FAILURE')).toBe('critical');
  });

  it('nodeStateTone: unrecognized/undefined values degrade to neutral, never throw', () => {
    expect(nodeStateTone(undefined)).toBe('neutral');
    expect(nodeStateTone('SOME_FUTURE_STATE')).toBe('neutral');
  });

  it('severityTone: NORMAL/LOW are healthy, MEDIUM is warning, HIGH/CRITICAL are critical', () => {
    expect(severityTone('NORMAL')).toBe('healthy');
    expect(severityTone('LOW')).toBe('healthy');
    expect(severityTone('MEDIUM')).toBe('warning');
    expect(severityTone('HIGH')).toBe('critical');
    expect(severityTone('CRITICAL')).toBe('critical');
  });
});
