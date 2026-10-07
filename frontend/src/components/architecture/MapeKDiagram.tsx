interface MapeKPhase {
  letter: string;
  name: string;
  description: string;
  implementedBy: string;
}

const PHASES: MapeKPhase[] = [
  { letter: 'M', name: 'Monitor', description: 'Collect telemetry from every simulated node.', implementedBy: 'IoT Core, SQS, SQS Consumer' },
  { letter: 'A', name: 'Analyze', description: 'Classify telemetry as healthy or anomalous.', implementedBy: 'Anomaly Detector Lambda' },
  { letter: 'P', name: 'Plan', description: 'Decide whether and how to remediate.', implementedBy: 'Remediation Trigger, Planner' },
  { letter: 'E', name: 'Execute', description: 'Carry out (or escalate) the remediation action.', implementedBy: 'Executor, Verifier' },
  { letter: 'K', name: 'Knowledge', description: 'Persist state/history/metrics and expose them for querying.', implementedBy: 'DynamoDB tables, CloudWatch metrics, Knowledge Query API' },
];

/** A compact visual explainer of the MAPE-K autonomic-computing loop
 * this project implements, and which real component owns each phase —
 * intended for both operator reference and project demonstrations. */
export function MapeKDiagram() {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
      {PHASES.map((phase) => (
        <div key={phase.name} className="flex flex-col gap-2 rounded-xl border border-surface-border bg-surface-850 p-4">
          <div className="flex items-center gap-2">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-500/10 font-mono text-sm font-bold text-brand-400">
              {phase.letter}
            </span>
            <p className="text-sm font-semibold text-slate-100">{phase.name}</p>
          </div>
          <p className="text-xs text-slate-400">{phase.description}</p>
          <p className="mt-auto text-[11px] text-slate-600">{phase.implementedBy}</p>
        </div>
      ))}
    </div>
  );
}
