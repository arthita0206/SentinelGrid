/**
 * DEMO DATA — NOT live AWS data, and NOT rendered on any live page.
 *
 * Modeled on the real simulator's default fleet shape (20 power / 20
 * water / 20 traffic — see config/simulation_config.yaml) and the real
 * NodeCurrentState shape the Knowledge Query API returns, but every
 * value here is synthetic and generated at build time, not measured.
 *
 * **Step 44 note**: through Step 43, `MOCK_NODES` (via `mockNodeById`)
 * was also used as `useNodeSummary`'s fallback when the live Node Detail
 * API call failed — that fallback was removed in Step 44 (see
 * `src/hooks/useNodeSummary.ts`'s own docstring for why: it made a
 * missing/failed node indistinguishable from a real one at a glance).
 * `MOCK_NODES` is now used ONLY as fixture data for component tests
 * (`src/test/components.test.tsx`'s `NodeTable` rendering test) — it is
 * never imported by any page or hook.
 */

import type { NodeCurrentState } from '@/types/api';
import type { FaultType, NodeState, Severity, Subsystem } from '@/types/domain';

interface MockNodeSpec {
  subsystem: Subsystem;
  count: number;
}

const FLEET: MockNodeSpec[] = [
  { subsystem: 'power', count: 20 },
  { subsystem: 'water', count: 20 },
  { subsystem: 'traffic', count: 20 },
];

const FAULT_TYPES_BY_SUBSYSTEM: Record<Subsystem, FaultType[]> = {
  power: ['NODE_OVERLOAD', 'SENSOR_DROPOUT', 'DATA_CORRUPTION'],
  water: ['NODE_OVERLOAD', 'NETWORK_PARTITION'],
  traffic: ['NODE_OVERLOAD', 'SENSOR_DROPOUT'],
};

const REASONS: Record<NodeState, string> = {
  HEALTHY: 'All telemetry fields are present and within the expected operating range.',
  FAULT_DETECTED: 'Reading exceeded the configured overload band for this field.',
  RECOVERING: 'Automatic remediation is in progress for this node.',
  RECOVERED: 'Simulated remediation action completed successfully.',
  ESCALATION_REQUIRED: 'Severity exceeds the automatic-remediation policy; escalated for manual intervention.',
  UNKNOWN: 'No recent telemetry has been recorded for this node.',
};

/** A small, fixed seeded PRNG (mulberry32) so demo data is stable across
 * reloads within a session — deliberately NOT `Math.random()`, so
 * component tests and visual review see consistent output. */
function mulberry32(seed: number) {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const rand = mulberry32(20260915);

function pick<T>(items: readonly T[]): T {
  return items[Math.floor(rand() * items.length)]!;
}

function isoMinutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString();
}

/** Distribution chosen to look like a plausible steady-state city: mostly
 * healthy, a handful of active faults/recoveries, one escalation — NOT a
 * claim about the real system's actual current state. */
function stateForIndex(i: number): NodeState {
  if (i % 23 === 0) return 'ESCALATION_REQUIRED';
  if (i % 11 === 0) return 'FAULT_DETECTED';
  if (i % 13 === 0) return 'RECOVERING';
  if (i % 17 === 0) return 'RECOVERED';
  return 'HEALTHY';
}

function buildNode(subsystem: Subsystem, index: number, globalIndex: number): NodeCurrentState {
  const nodeId = `${subsystem}-${String(index).padStart(3, '0')}`;
  const state = stateForIndex(globalIndex);
  const hasFault = state === 'FAULT_DETECTED' || state === 'ESCALATION_REQUIRED' || state === 'RECOVERED';
  const faultType = hasFault ? pick(FAULT_TYPES_BY_SUBSYSTEM[subsystem]) : null;
  const severity: Severity | null = state === 'ESCALATION_REQUIRED' ? 'HIGH' : hasFault ? 'MEDIUM' : null;
  const minutesAgo = Math.floor(rand() * 45) + 1;

  return {
    node_id: nodeId,
    subsystem,
    status: state === 'UNKNOWN' ? 'OFFLINE' : 'ONLINE',
    state,
    last_telemetry_timestamp: isoMinutesAgo(minutesAgo),
    last_state_update: isoMinutesAgo(Math.max(0, minutesAgo - 1)),
    reason: REASONS[state],
    fault_type: faultType,
    fault_severity: severity,
  };
}

export const MOCK_NODES: NodeCurrentState[] = FLEET.flatMap(({ subsystem, count }, fleetIndex) =>
  Array.from({ length: count }, (_, i) => buildNode(subsystem, i + 1, fleetIndex * count + i)),
);
