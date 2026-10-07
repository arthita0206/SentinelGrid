import { cn } from '@/lib/utils';

interface FlowStep {
  label: string;
  detail: string;
  /** Which MAPE-K phase this step belongs to, for the small phase tag —
   * omitted for steps outside the control loop itself (the simulator,
   * the dashboard). */
  phase?: 'Monitor' | 'Analyze' | 'Plan' | 'Execute' | 'Knowledge';
}

/** The exact pipeline this repository implements — see the top-level
 * README and infrastructure/README.md. Every step listed here
 * corresponds to a real, named AWS resource or code module already in
 * the repository; nothing here is aspirational. The SRS originally
 * specified Kinesis for telemetry ingestion — the deployed pipeline uses
 * SQS instead (Kinesis access was blocked by an AWS account-level
 * restriction; see infrastructure/README.md's
 * "telemetry-kinesis-stream.yaml (Step 24 — local design complete, AWS
 * deployment BLOCKED)" section for that history). Kinesis is
 * deliberately NOT shown below, since it was never actually deployed. */
const FLOW: FlowStep[] = [
  { label: 'City Telemetry Simulator', detail: 'simulator/ — local Python process, publishes synthetic readings.' },
  { label: 'AWS IoT Core', detail: 'Topic: sentinelgrid/telemetry', phase: 'Monitor' },
  { label: 'DynamoDB — Telemetry History', detail: 'SentinelGridTelemetry — every raw telemetry reading, written directly by an IoT Topic Rule.', phase: 'Monitor' },
  { label: 'SQS (SentinelGridTelemetryQueue)', detail: 'Durable buffer; DLQ on repeated failure.', phase: 'Monitor' },
  { label: 'SQS Consumer', detail: 'SentinelGridSqsTelemetryConsumer — bridges SQS to the Analyzer.', phase: 'Monitor' },
  { label: 'Anomaly Detector', detail: 'SentinelGridAnomalyDetector — deterministic rule-based classification.', phase: 'Analyze' },
  { label: 'DynamoDB — Anomaly Events', detail: 'SentinelGridAnomalyEvents — append-only fault history.', phase: 'Knowledge' },
  { label: 'Remediation Trigger', detail: 'SentinelGridRemediationTrigger — starts the workflow, idempotently.', phase: 'Plan' },
  { label: 'Step Functions', detail: 'SentinelGridRemediationWorkflow orchestrates Planner → Executor → Verifier.', phase: 'Plan' },
  { label: 'Planner → Executor → Verifier', detail: 'Decide, simulate execution of, and verify a remediation action.', phase: 'Execute' },
  { label: 'DynamoDB — Recovery Events', detail: 'SentinelGridRecoveryEvents — append-only recovery/escalation history.', phase: 'Knowledge' },
  { label: 'DynamoDB — Current Node State', detail: 'SentinelGridNodeState — one latest-known-state item per node.', phase: 'Knowledge' },
  { label: 'CloudWatch Metrics', detail: 'SentinelGrid namespace — 7 metrics (FaultDetectionCount, DetectionLatency, RecoveryAttemptCount/Success/Failure, RecoveryLatency, UnresolvedFaultCount), published by the Analyzer/Executor/Planner/Verifier.', phase: 'Knowledge' },
  { label: 'CloudWatch Dashboard & SNS Escalation', detail: 'SentinelGrid-Observability dashboard (read-only) and a CloudWatch Alarm on UnresolvedFaultCount publishing to the SentinelGrid-UnresolvedFaults SNS topic.', phase: 'Knowledge' },
  { label: 'Knowledge Query API', detail: 'API Gateway HTTP API + SentinelGridKnowledgeQuery Lambda — read-only over DynamoDB (node state, anomaly/recovery events) and CloudWatch metrics.', phase: 'Knowledge' },
  { label: 'SentinelGrid Operations Dashboard', detail: 'This application — a consumer of the Knowledge Query API.' },
];

const PHASE_COLOR: Record<NonNullable<FlowStep['phase']>, string> = {
  Monitor: 'border-status-info/40 text-status-info',
  Analyze: 'border-status-warning/40 text-status-warning',
  Plan: 'border-brand-400/40 text-brand-400',
  Execute: 'border-status-healthy/40 text-status-healthy',
  Knowledge: 'border-status-neutral/40 text-status-neutral',
};

export function ArchitectureDiagram() {
  return (
    <ol className="relative flex flex-col gap-0">
      {FLOW.map((step, index) => (
        <li key={step.label} className="relative flex gap-4 pb-6 last:pb-0">
          {index < FLOW.length - 1 ? (
            <span className="absolute top-8 left-[15px] h-[calc(100%-1.5rem)] w-px bg-surface-border" aria-hidden="true" />
          ) : null}
          <span className="relative z-10 mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-surface-border bg-surface-850 text-xs font-semibold text-slate-400">
            {index + 1}
          </span>
          <div className="flex-1 rounded-lg border border-surface-border bg-surface-850 px-4 py-3">
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-sm font-semibold text-slate-100">{step.label}</p>
              {step.phase ? (
                <span className={cn('rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide uppercase', PHASE_COLOR[step.phase])}>
                  {step.phase}
                </span>
              ) : null}
            </div>
            <p className="mt-1 text-xs text-slate-500">{step.detail}</p>
          </div>
        </li>
      ))}
    </ol>
  );
}
