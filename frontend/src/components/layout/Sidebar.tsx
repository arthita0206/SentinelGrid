import { NavLink } from 'react-router-dom';
import { AnalyticsIcon, ArchitectureIcon, DashboardIcon, EventsIcon, NodesIcon, SentinelMark } from '@/components/icons';
import { cn } from '@/lib/utils';

const NAV_ITEMS = [
  { to: '/dashboard', label: 'Dashboard', icon: DashboardIcon },
  { to: '/nodes', label: 'Nodes', icon: NodesIcon },
  { to: '/events', label: 'Events', icon: EventsIcon },
  { to: '/analytics', label: 'Analytics', icon: AnalyticsIcon },
  { to: '/architecture', label: 'Architecture', icon: ArchitectureIcon },
];

interface SidebarProps {
  /** Controls mobile/tablet slide-in visibility; ignored at desktop
   * widths where the sidebar is always visible. */
  open: boolean;
  onClose: () => void;
}

export function Sidebar({ open, onClose }: SidebarProps) {
  return (
    <>
      {/* Mobile/tablet scrim */}
      {open ? (
        <button
          type="button"
          aria-label="Close navigation"
          onClick={onClose}
          className="fixed inset-0 z-30 bg-black/60 lg:hidden"
        />
      ) : null}

      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-64 shrink-0 flex-col border-r border-surface-border bg-surface-900',
          'transition-transform duration-200 lg:static lg:z-auto lg:translate-x-0',
          open ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex h-16 items-center gap-2.5 border-b border-surface-border px-5">
          <SentinelMark className="h-8 w-8 shrink-0" />
          <div className="leading-tight">
            <p className="text-sm font-bold tracking-wide text-slate-50">SentinelGrid</p>
            <p className="text-[11px] text-slate-500">Operations Console</p>
          </div>
        </div>

        <nav aria-label="Primary" className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
          {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              onClick={onClose}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors',
                  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400',
                  isActive
                    ? 'bg-brand-500/10 text-brand-400'
                    : 'text-slate-400 hover:bg-surface-800 hover:text-slate-100',
                )
              }
            >
              <Icon className="h-4.5 w-4.5 shrink-0" />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-surface-border px-4 py-4">
          <p className="text-[11px] leading-relaxed text-slate-600">
            Academic self-healing smart-city architecture. Simulated telemetry, real AWS backend.
          </p>
        </div>
      </aside>
    </>
  );
}
