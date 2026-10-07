interface LoadingStateProps {
  label?: string;
  /** Number of skeleton rows to render, for list/table contexts. Omit
   * for a plain centered spinner. */
  rows?: number;
}

export function LoadingState({ label = 'Loading…', rows }: LoadingStateProps) {
  if (rows) {
    return (
      <div className="space-y-2" role="status" aria-label={label}>
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="h-10 animate-pulse rounded-lg bg-surface-800" />
        ))}
        <span className="sr-only">{label}</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12" role="status" aria-label={label}>
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-surface-600 border-t-brand-400" aria-hidden="true" />
      <p className="text-xs text-slate-500">{label}</p>
    </div>
  );
}
