/**
 * Domain vocabulary mirrored from the deployed SentinelGrid backend.
 *
 * These values are NOT invented for the UI — they match the backend's
 * own closed vocabularies exactly, so badges/filters never show a state
 * the API could not actually produce:
 *   - Subsystem: shared/telemetry.py producers (power/water/traffic)
 *   - NodeState: shared/node_state_store.py `NodeState` enum
 *   - FaultType: lambda_functions/anomaly_detector/detector.py `FaultType`
 *   - Severity: lambda_functions/anomaly_detector/detector.py `Severity`
 *   - RecoveryStatus: lambda_functions/remediation/recovery_event_store.py
 *     `RecoveryStatus`
 */

export type Subsystem = 'power' | 'water' | 'traffic';

export const SUBSYSTEMS: Subsystem[] = ['power', 'water', 'traffic'];

export type TelemetryStatus = 'ONLINE' | 'OFFLINE';

export type NodeState =
  | 'HEALTHY'
  | 'FAULT_DETECTED'
  | 'RECOVERING'
  | 'RECOVERED'
  | 'ESCALATION_REQUIRED'
  | 'UNKNOWN';

export const NODE_STATES: NodeState[] = [
  'HEALTHY',
  'FAULT_DETECTED',
  'RECOVERING',
  'RECOVERED',
  'ESCALATION_REQUIRED',
  'UNKNOWN',
];

export type FaultType =
  | 'SENSOR_DROPOUT'
  | 'NETWORK_PARTITION'
  | 'DATA_CORRUPTION'
  | 'NODE_OVERLOAD'
  | 'INVALID_INPUT';

/** Excludes detector.py's `FaultType.NONE` deliberately — `NONE` is
 * never published as a CloudWatch metric dimension value (Step 43's
 * `GET /metrics` `fault_type` filter rejects it), so it does not belong
 * in a UI vocabulary meant for filtering metrics/events by a REAL fault
 * type. */
export const FAULT_TYPES: FaultType[] = ['SENSOR_DROPOUT', 'NETWORK_PARTITION', 'DATA_CORRUPTION', 'NODE_OVERLOAD', 'INVALID_INPUT'];

export type Severity = 'NORMAL' | 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';

export type RecoveryStatus = 'SUCCESS' | 'FAILURE' | 'ESCALATION_REQUIRED';

/** A single logical "status meaning" every badge/indicator maps onto —
 * this is the UI's own small vocabulary, kept separate from the
 * backend's literal enum values above so a new backend value (e.g. a
 * future NodeState) only needs one new mapping entry, not a UI rewrite. */
export type StatusTone = 'healthy' | 'warning' | 'critical' | 'info' | 'neutral';
