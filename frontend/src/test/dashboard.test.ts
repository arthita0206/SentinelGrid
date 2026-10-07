import { describe, expect, it } from 'vitest';
import { deriveDashboardStats, deriveSubsystemStats, deriveSystemStatus } from '@/lib/dashboard';
import type { NodeCurrentState } from '@/types/api';
import type { NodeState, Subsystem } from '@/types/domain';

function node(overrides: Partial<NodeCurrentState> & { node_id: string; subsystem: Subsystem; state: NodeState }): NodeCurrentState {
  return {
    status: 'ONLINE',
    last_telemetry_timestamp: '2026-01-01T00:00:00+00:00',
    last_state_update: '2026-01-01T00:00:01+00:00',
    reason: 'ok',
    ...overrides,
  };
}

describe('deriveDashboardStats', () => {
  it('counts total nodes', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'HEALTHY' }),
      node({ node_id: 'b', subsystem: 'water', state: 'HEALTHY' }),
    ];
    expect(deriveDashboardStats(nodes).totalNodes).toBe(2);
  });

  it('counts HEALTHY nodes only for healthyNodes (not RECOVERED)', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'HEALTHY' }),
      node({ node_id: 'b', subsystem: 'power', state: 'RECOVERED' }),
    ];
    expect(deriveDashboardStats(nodes).healthyNodes).toBe(1);
  });

  it('counts FAULT_DETECTED + RECOVERING + ESCALATION_REQUIRED as activeFaults', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'FAULT_DETECTED' }),
      node({ node_id: 'b', subsystem: 'power', state: 'RECOVERING' }),
      node({ node_id: 'c', subsystem: 'power', state: 'ESCALATION_REQUIRED' }),
    ];
    expect(deriveDashboardStats(nodes).activeFaults).toBe(3);
  });

  it('does NOT count RECOVERED as an active fault', () => {
    const nodes = [node({ node_id: 'a', subsystem: 'power', state: 'RECOVERED' })];
    expect(deriveDashboardStats(nodes).activeFaults).toBe(0);
  });

  it('counts RECOVERED nodes as recoveredNodes (a current-state snapshot, not a historical count)', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'RECOVERED' }),
      node({ node_id: 'b', subsystem: 'power', state: 'RECOVERED' }),
      node({ node_id: 'c', subsystem: 'power', state: 'HEALTHY' }),
    ];
    expect(deriveDashboardStats(nodes).recoveredNodes).toBe(2);
  });

  it('UNKNOWN nodes count toward totalNodes but not healthy/fault/recovered', () => {
    const nodes = [node({ node_id: 'a', subsystem: 'power', state: 'UNKNOWN' })];
    const stats = deriveDashboardStats(nodes);
    expect(stats.totalNodes).toBe(1);
    expect(stats.healthyNodes).toBe(0);
    expect(stats.activeFaults).toBe(0);
    expect(stats.recoveredNodes).toBe(0);
  });

  it('returns all zeros for an empty node list', () => {
    expect(deriveDashboardStats([])).toEqual({ totalNodes: 0, healthyNodes: 0, activeFaults: 0, recoveredNodes: 0 });
  });

  it('handles a realistic mixed fleet', () => {
    const nodes: NodeCurrentState[] = [
      node({ node_id: 'a', subsystem: 'power', state: 'HEALTHY' }),
      node({ node_id: 'b', subsystem: 'power', state: 'HEALTHY' }),
      node({ node_id: 'c', subsystem: 'water', state: 'FAULT_DETECTED' }),
      node({ node_id: 'd', subsystem: 'traffic', state: 'RECOVERING' }),
      node({ node_id: 'e', subsystem: 'traffic', state: 'ESCALATION_REQUIRED' }),
      node({ node_id: 'f', subsystem: 'power', state: 'RECOVERED' }),
      node({ node_id: 'g', subsystem: 'water', state: 'UNKNOWN' }),
    ];
    expect(deriveDashboardStats(nodes)).toEqual({
      totalNodes: 7,
      healthyNodes: 2,
      activeFaults: 3,
      recoveredNodes: 1,
    });
  });
});

describe('deriveSubsystemStats', () => {
  it('always returns exactly three entries: power, water, traffic', () => {
    const stats = deriveSubsystemStats([]);
    expect(stats.map((s) => s.subsystem)).toEqual(['power', 'water', 'traffic']);
  });

  it('reports an honest zero state for a subsystem with no nodes', () => {
    const nodes = [node({ node_id: 'a', subsystem: 'power', state: 'HEALTHY' })];
    const stats = deriveSubsystemStats(nodes);
    const traffic = stats.find((s) => s.subsystem === 'traffic')!;
    expect(traffic.nodeCount).toBe(0);
    expect(traffic.healthyCount).toBe(0);
    expect(traffic.faultCount).toBe(0);
    expect(traffic.tone).toBe('neutral');
  });

  it('computes per-subsystem node/healthy/fault counts independently', () => {
    const nodes = [
      node({ node_id: 'p1', subsystem: 'power', state: 'HEALTHY' }),
      node({ node_id: 'p2', subsystem: 'power', state: 'FAULT_DETECTED' }),
      node({ node_id: 'w1', subsystem: 'water', state: 'HEALTHY' }),
      node({ node_id: 'w2', subsystem: 'water', state: 'HEALTHY' }),
    ];
    const stats = deriveSubsystemStats(nodes);
    const power = stats.find((s) => s.subsystem === 'power')!;
    const water = stats.find((s) => s.subsystem === 'water')!;
    expect(power).toMatchObject({ nodeCount: 2, healthyCount: 1, faultCount: 1 });
    expect(water).toMatchObject({ nodeCount: 2, healthyCount: 2, faultCount: 0 });
  });

  it('tone is healthy when every node in the subsystem is healthy', () => {
    const nodes = [node({ node_id: 'a', subsystem: 'power', state: 'HEALTHY' })];
    const power = deriveSubsystemStats(nodes).find((s) => s.subsystem === 'power')!;
    expect(power.tone).toBe('healthy');
  });

  it('tone is critical when a subsystem has faults and zero healthy nodes', () => {
    const nodes = [node({ node_id: 'a', subsystem: 'power', state: 'FAULT_DETECTED' })];
    const power = deriveSubsystemStats(nodes).find((s) => s.subsystem === 'power')!;
    expect(power.tone).toBe('critical');
  });

  it('tone is warning when a subsystem has some faults but also some healthy nodes', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'HEALTHY' }),
      node({ node_id: 'b', subsystem: 'power', state: 'FAULT_DETECTED' }),
    ];
    const power = deriveSubsystemStats(nodes).find((s) => s.subsystem === 'power')!;
    expect(power.tone).toBe('warning');
  });
});

describe('deriveSystemStatus', () => {
  it('returns Unknown (neutral) for an empty node list -- never fabricates Healthy', () => {
    expect(deriveSystemStatus([])).toEqual({ label: 'Unknown', tone: 'neutral' });
  });

  it('returns Healthy when every node is HEALTHY or RECOVERED', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'HEALTHY' }),
      node({ node_id: 'b', subsystem: 'water', state: 'RECOVERED' }),
    ];
    expect(deriveSystemStatus(nodes)).toEqual({ label: 'Healthy', tone: 'healthy' });
  });

  it('returns Unknown when the only non-healthy state present is UNKNOWN', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'HEALTHY' }),
      node({ node_id: 'b', subsystem: 'water', state: 'UNKNOWN' }),
    ];
    expect(deriveSystemStatus(nodes)).toEqual({ label: 'Unknown', tone: 'neutral' });
  });

  it('returns Recovering when a node is RECOVERING and nothing worse is present', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'HEALTHY' }),
      node({ node_id: 'b', subsystem: 'water', state: 'RECOVERING' }),
    ];
    expect(deriveSystemStatus(nodes)).toEqual({ label: 'Recovering', tone: 'warning' });
  });

  it('returns Fault Detected when a node is FAULT_DETECTED, outranking RECOVERING', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'RECOVERING' }),
      node({ node_id: 'b', subsystem: 'water', state: 'FAULT_DETECTED' }),
    ];
    expect(deriveSystemStatus(nodes)).toEqual({ label: 'Fault Detected', tone: 'critical' });
  });

  it('returns Escalation Required when a node is ESCALATION_REQUIRED, outranking everything else', () => {
    const nodes = [
      node({ node_id: 'a', subsystem: 'power', state: 'FAULT_DETECTED' }),
      node({ node_id: 'b', subsystem: 'water', state: 'RECOVERING' }),
      node({ node_id: 'c', subsystem: 'traffic', state: 'ESCALATION_REQUIRED' }),
    ];
    expect(deriveSystemStatus(nodes)).toEqual({ label: 'Escalation Required', tone: 'critical' });
  });

  it('is deterministic regardless of array order', () => {
    const a = [
      node({ node_id: 'x', subsystem: 'power', state: 'ESCALATION_REQUIRED' }),
      node({ node_id: 'y', subsystem: 'water', state: 'FAULT_DETECTED' }),
    ];
    const b = [...a].reverse();
    expect(deriveSystemStatus(a)).toEqual(deriveSystemStatus(b));
  });
});
