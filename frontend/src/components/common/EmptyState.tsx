import type { ReactNode } from 'react';

interface EmptyStateProps {
  title: string;
  description?: string;
  icon?: ReactNode;
  action?: ReactNode;
}

/** Shown when a request succeeded but returned nothing — deliberately
 * distinct from ErrorState (a failure) and LoadingState (in progress). */
export function EmptyState({ title, description, icon, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-surface-border px-6 py-12 text-center">
      {icon ? <span className="mb-1 text-slate-600">{icon}</span> : null}
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {description ? <p className="max-w-sm text-xs text-slate-500">{description}</p> : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}
