interface ErrorStateProps {
  title?: string;
  message?: string;
  onRetry?: () => void;
}

/** Shown when a request genuinely failed. No page in this app falls
 * back to fabricated demo data on a failed live call (see
 * DataSourceTag) — this is the honest state shown instead. */
export function ErrorState({ title = 'Something went wrong', message, onRetry }: ErrorStateProps) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center gap-2 rounded-xl border border-status-critical/30 bg-status-critical-muted/40 px-6 py-10 text-center"
    >
      <p className="text-sm font-medium text-status-critical">{title}</p>
      {message ? <p className="max-w-sm text-xs text-slate-400">{message}</p> : null}
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded-lg border border-surface-border bg-surface-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-surface-700"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
