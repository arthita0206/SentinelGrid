/**
 * Static metadata (name/unit/description) for the seven approved
 * SentinelGrid CloudWatch metrics — REAL and accurate, matching
 * `shared/metrics.py`'s `MetricName` contract and
 * `infrastructure/README.md`'s own metric table exactly.
 *
 * As of Step 43, `GET /metrics` returns the live numeric data itself
 * (see `src/hooks/useMetrics.ts`) — this file no longer contains any
 * synthetic series (Step 39's `demoSeries()` helper was removed once
 * `AnalyticsPage` no longer needed it; this file was renamed from
 * `mockMetrics.ts` at the same time, since a file called "mock" that
 * contains zero mock data would contradict this project's own strict
 * live/demo labeling conventions).
 */

import type { MetricUnit } from '@/types/api';

export interface MetricDefinition {
  name: string;
  unit: MetricUnit;
  description: string;
  /** Which SRS requirement this metric is evidence for, if any. */
  srsRef?: string;
}

/** Order here is also the canonical display order — matches
 * `lambda_functions/knowledge_query/metrics_service.py`'s own
 * `APPROVED_METRICS` order exactly. */
export const METRIC_DEFINITIONS: MetricDefinition[] = [
  {
    name: 'FaultDetectionCount',
    unit: 'Count',
    description: 'Count of detection observations — anomalies classified as detected=True by the Analyzer, by Subsystem/FaultType.',
    srsRef: 'FR-7.1',
  },
  {
    name: 'DetectionLatency',
    unit: 'Milliseconds',
    description:
      'Raw detection-latency measurements: telemetry timestamp to analyzer detection. Not an aggregate SLA guarantee. See docs/performance-validation.md for measured baselines.',
    srsRef: 'NFR-1',
  },
  {
    name: 'RecoveryAttemptCount',
    unit: 'Count',
    description: 'Count of automatic remediation attempts submitted to the Executor.',
  },
  {
    name: 'RecoverySuccessCount',
    unit: 'Count',
    description: 'Count of remediation executions that completed successfully.',
  },
  {
    name: 'RecoveryFailureCount',
    unit: 'Count',
    description: 'Count of remediation executions that failed or were rejected.',
  },
  {
    name: 'RecoveryLatency',
    unit: 'Milliseconds',
    description:
      'Detection timestamp to recovery completion — combines detection AND remediation time. This is NOT isolated remediation execution time, and is not automatically "MTTR." See docs/performance-validation.md.',
    srsRef: 'NFR-2',
  },
  {
    name: 'UnresolvedFaultCount',
    unit: 'Count',
    description:
      'An unresolved/escalation observation count — faults that required escalation rather than automatic remediation. This is NOT a continuously-maintained active-fault gauge: it never decrements, and an OK alarm state elsewhere does not mean a fault counted here was resolved.',
  },
];
