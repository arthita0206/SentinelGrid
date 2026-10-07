import { SUBSYSTEMS, type NodeState, type StatusTone, type Subsystem } from '@/types/domain';
import type { NodeCurrentState } from '@/types/api';

/**
 * Pure, deterministic derivation helpers for the Dashboard page (Step
 * 41). Each function takes the live `GET /nodes` result (or an empty
 * array) and computes a UI-ready summary — no fetching, no React, no
 * side effects, so these are fully unit-testable without mocking
 * `fetch`. `DashboardPage` is the only caller; keep it that way rather
 * than duplicating this logic inline in a component.
 */

// --- Dashboard summary cards ---

export interface DashboardStats {
  totalNodes: number;
  /** Nodes currently in state HEALTHY. Does NOT include RECOVERED —
   * "recovered" and "currently healthy" are different facts about a
   * node (see `recoveredNodes` below). */
  healthyNodes: number;
  /** Nodes in an active/problem state: FAULT_DETECTED, RECOVERING, or
   * ESCALATION_REQUIRED. RECOVERED is deliberately EXCLUDED — a
   * recovered node is no longer an active problem, per Step 41's
   * explicit instruction not to count it as a fault. */
  activeFaults: number;
  /** Nodes CURRENTLY in state RECOVERED — a present-tense snapshot from
   * SentinelGridNodeState, NOT a historical count of recovery events.
   * `GET /nodes` has no historical event data at all (see
   * frontend/README.md's "Current state vs. historical events"
   * section) — this number will not match, and must never be labeled
   * as, a count of recovery events from SentinelGridRecoveryEvents. */
  recoveredNodes: number;
}

export function deriveDashboardStats(nodes: NodeCurrentState[]): DashboardStats {
  let healthyNodes = 0;
  let activeFaults = 0;
  let recoveredNodes = 0;

  for (const node of nodes) {
    switch (node.state) {
      case 'HEALTHY':
        healthyNodes += 1;
        break;
      case 'RECOVERED':
        recoveredNodes += 1;
        break;
      case 'FAULT_DETECTED':
      case 'RECOVERING':
      case 'ESCALATION_REQUIRED':
        activeFaults += 1;
        break;
      // 'UNKNOWN' and any future/unrecognized value: counted in
      // totalNodes only, deliberately not folded into any of the three
      // more specific buckets above (it is neither confirmed healthy
      // nor a confirmed active problem).
      default:
        break;
    }
  }

  return { totalNodes: nodes.length, healthyNodes, activeFaults, recoveredNodes };
}

// --- Subsystem cards ---

export interface SubsystemStats {
  subsystem: Subsystem;
  nodeCount: number;
  healthyCount: number;
  /** Same active-fault definition as `DashboardStats.activeFaults`
   * (FAULT_DETECTED + RECOVERING + ESCALATION_REQUIRED), scoped to this
   * one subsystem. */
  faultCount: number;
  tone: StatusTone;
}

function subsystemTone(nodeCount: number, healthyCount: number, faultCount: number): StatusTone {
  if (nodeCount === 0) return 'neutral'; // nothing to report -- not the same as "healthy"
  if (faultCount > 0) return healthyCount === 0 ? 'critical' : 'warning';
  return 'healthy';
}

/** Always returns exactly one entry per SUBSYSTEMS ('power', 'water',
 * 'traffic'), even when a subsystem has zero live nodes — Step 41's
 * explicit requirement: "Do not assume that all three subsystems
 * necessarily have data... display an honest empty/zero state," never
 * omit the card outright. */
export function deriveSubsystemStats(nodes: NodeCurrentState[]): SubsystemStats[] {
  return SUBSYSTEMS.map((subsystem) => {
    const subsystemNodes = nodes.filter((n) => n.subsystem === subsystem);
    const { healthyNodes, activeFaults } = deriveDashboardStats(subsystemNodes);
    return {
      subsystem,
      nodeCount: subsystemNodes.length,
      healthyCount: healthyNodes,
      faultCount: activeFaults,
      tone: subsystemTone(subsystemNodes.length, healthyNodes, activeFaults),
    };
  });
}

// --- Overall system status ---

export type SystemStatusLabel = 'Healthy' | 'Recovering' | 'Fault Detected' | 'Escalation Required' | 'Unknown';

export interface SystemStatus {
  label: SystemStatusLabel;
  tone: StatusTone;
}

/**
 * Derives one overall status label from the live node fleet — a
 * DASHBOARD-LEVEL INTERPRETATION of current `SentinelGridNodeState`
 * records, not an AWS/CloudWatch health check and not infrastructure
 * status of any kind (API connectivity is shown separately — see
 * `DashboardPage`'s own "API Connected"/"API Unavailable" indicator).
 *
 * Deterministic precedence, most to least severe (documented here and
 * in frontend/README.md — this exact order is the contract):
 *   1. ANY node ESCALATION_REQUIRED  -> "Escalation Required" (critical)
 *   2. ANY node FAULT_DETECTED       -> "Fault Detected"      (critical)
 *   3. ANY node RECOVERING           -> "Recovering"          (warning)
 *   4. ANY node UNKNOWN (and none of the above) -> "Unknown"  (neutral)
 *   5. No nodes at all, OR every node is HEALTHY/RECOVERED    -> "Healthy" / "Unknown"
 *
 * Rationale for this exact order: escalation means a human must act —
 * the single most urgent condition an operator needs to see first, so
 * it outranks everything else regardless of how many other nodes are
 * fine. An untreated active fault outranks one that is already
 * RECOVERING (remediation in progress is a less urgent state than "no
 * remediation has started yet"). UNKNOWN nodes are neither a confirmed
 * problem nor confirmed healthy, so they sit below every genuine
 * problem state but above a false claim of "Healthy" when the fleet's
 * health has not actually been confirmed.
 *
 * An EMPTY node list returns "Unknown" (tone `neutral`), never
 * "Healthy" — asserting the fleet is healthy when zero nodes were
 * actually observed would be a fabricated claim.
 */
export function deriveSystemStatus(nodes: NodeCurrentState[]): SystemStatus {
  if (nodes.length === 0) {
    return { label: 'Unknown', tone: 'neutral' };
  }

  const states = new Set<NodeState>(nodes.map((n) => n.state));

  if (states.has('ESCALATION_REQUIRED')) return { label: 'Escalation Required', tone: 'critical' };
  if (states.has('FAULT_DETECTED')) return { label: 'Fault Detected', tone: 'critical' };
  if (states.has('RECOVERING')) return { label: 'Recovering', tone: 'warning' };
  if (states.has('UNKNOWN')) return { label: 'Unknown', tone: 'neutral' };

  // Every observed node is HEALTHY and/or RECOVERED.
  return { label: 'Healthy', tone: 'healthy' };
}
