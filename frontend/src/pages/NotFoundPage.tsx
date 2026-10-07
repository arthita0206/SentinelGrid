import { Link } from 'react-router-dom';
import { SentinelMark } from '@/components/icons';

export function NotFoundPage() {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-4 bg-surface-950 px-6 text-center">
      <SentinelMark className="h-12 w-12 opacity-60" />
      <p className="text-5xl font-bold text-slate-100">404</p>
      <p className="text-sm text-slate-400">This page doesn&apos;t exist in the SentinelGrid console.</p>
      <Link
        to="/dashboard"
        className="mt-2 rounded-lg border border-surface-border bg-surface-800 px-4 py-2 text-sm font-medium text-slate-200 hover:bg-surface-700"
      >
        Return to Dashboard
      </Link>
    </div>
  );
}
