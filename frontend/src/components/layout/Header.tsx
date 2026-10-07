import { useLocation } from 'react-router-dom';
import { isApiConfigured } from '@/services/api';
import { StatusBadge } from '@/components/common/StatusBadge';

const ROUTE_TITLES: Record<string, string> = {
  '/dashboard': 'Dashboard',
  '/nodes': 'Nodes',
  '/events': 'Events',
  '/analytics': 'Analytics',
  '/architecture': 'Architecture',
};

function currentTitle(pathname: string): string {
  if (ROUTE_TITLES[pathname]) return ROUTE_TITLES[pathname]!;
  if (pathname.startsWith('/nodes/')) return 'Node Detail';
  return 'SentinelGrid';
}

interface HeaderProps {
  onMenuClick: () => void;
}

/** Slim, persistent top bar — mobile nav toggle, current section name,
 * and a small always-visible indicator of whether a live API base URL
 * is configured at all (not whether a specific call succeeded — that is
 * surfaced per-page via DataSourceTag). */
export function Header({ onMenuClick }: HeaderProps) {
  const location = useLocation();
  const configured = isApiConfigured();

  return (
    <header className="sticky top-0 z-20 flex h-16 shrink-0 items-center justify-between border-b border-surface-border bg-surface-950/90 px-4 backdrop-blur sm:px-6">
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onMenuClick}
          aria-label="Open navigation"
          className="rounded-lg p-2 text-slate-400 hover:bg-surface-800 hover:text-slate-100 lg:hidden"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} className="h-5 w-5">
            <path strokeLinecap="round" d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <h2 className="text-sm font-semibold text-slate-200">{currentTitle(location.pathname)}</h2>
      </div>

      <div className="flex items-center gap-3">
        <StatusBadge
          tone={configured ? 'info' : 'neutral'}
          label={configured ? 'API configured' : 'API not configured'}
          className="hidden sm:inline-flex"
        />
      </div>
    </header>
  );
}
