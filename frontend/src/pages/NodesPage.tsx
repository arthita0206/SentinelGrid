import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { DataSourceTag } from '@/components/common/DataSourceTag';
import { ErrorState } from '@/components/common/ErrorState';
import { LoadingState } from '@/components/common/LoadingState';
import { NodeTable } from '@/components/common/NodeTable';
import { PageHeader } from '@/components/common/PageHeader';
import { SearchIcon } from '@/components/icons';
import { useNodes } from '@/hooks/useNodes';
import { humanize } from '@/lib/utils';
import { NODE_STATES, SUBSYSTEMS, type NodeState, type Subsystem } from '@/types/domain';

type SortKey = 'node_id' | 'state' | 'last_telemetry_timestamp';

// The fleet is currently expected to stay at or below SRS NFR-3's
// 500-node target — a single bounded GET /nodes?limit=500 call is
// therefore enough to show the whole fleet in one page (no client-side
// pagination is implemented, matching the backend's own bounded-Scan
// design — see lambda_functions/knowledge_query/query_service.py's
// list_nodes() docstring).
const FLEET_FETCH_LIMIT = 500;

export function NodesPage() {
  const { nodes, loading, error, loaded, refetch } = useNodes({ limit: FLEET_FETCH_LIMIT, order: 'asc' });

  const [searchParams, setSearchParams] = useSearchParams();
  const subsystemFilter = (searchParams.get('subsystem') as Subsystem | null) ?? 'all';
  const [stateFilter, setStateFilter] = useState<NodeState | 'all'>('all');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('node_id');

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    return nodes
      .filter((node) => {
        if (subsystemFilter !== 'all' && node.subsystem !== subsystemFilter) return false;
        if (stateFilter !== 'all' && node.state !== stateFilter) return false;
        if (query && !node.node_id.toLowerCase().includes(query)) return false;
        return true;
      })
      .sort((a, b) => {
        if (sortKey === 'last_telemetry_timestamp') {
          return new Date(b.last_telemetry_timestamp).getTime() - new Date(a.last_telemetry_timestamp).getTime();
        }
        return String(a[sortKey]).localeCompare(String(b[sortKey]));
      });
  }, [nodes, subsystemFilter, stateFilter, search, sortKey]);

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Nodes"
        description={loaded ? `${filtered.length} of ${nodes.length} nodes shown.` : 'Loading the current node fleet…'}
        actions={
          loaded ? (
            <DataSourceTag isMock={false} />
          ) : error ? (
            // 'pending', not the default 'demo': the error state below
            // shows no fabricated data at all, just an honest ErrorState.
            <DataSourceTag isMock variant="pending" reason={error.message} />
          ) : null
        }
      />

      {loading ? (
        <LoadingState label="Loading node fleet…" rows={8} />
      ) : error ? (
        <ErrorState
          title="Could not load the node fleet"
          message={`${error.message} If this is a CORS error, confirm the API's FrontendOrigin matches this page's origin — see frontend/README.md.`}
          onRetry={refetch}
        />
      ) : (
        <>
          <div className="flex flex-col gap-3 rounded-xl border border-surface-border bg-surface-850 p-4 sm:flex-row sm:items-center sm:flex-wrap">
            <label className="relative flex-1 sm:min-w-[220px]">
              <span className="sr-only">Search by node ID</span>
              <SearchIcon className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-slate-500" />
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search node ID…"
                className="w-full rounded-lg border border-surface-border bg-surface-900 py-2 pr-3 pl-9 text-sm text-slate-200 placeholder:text-slate-600 focus:border-brand-500 focus:outline-none"
              />
            </label>

            <label className="flex items-center gap-2 text-xs text-slate-400">
              Subsystem
              <select
                value={subsystemFilter}
                onChange={(e) => {
                  const value = e.target.value;
                  setSearchParams(value === 'all' ? {} : { subsystem: value });
                }}
                className="rounded-lg border border-surface-border bg-surface-900 px-2.5 py-1.5 text-sm text-slate-200 focus:border-brand-500 focus:outline-none"
              >
                <option value="all">All</option>
                {SUBSYSTEMS.map((s) => (
                  <option key={s} value={s}>
                    {humanize(s)}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex items-center gap-2 text-xs text-slate-400">
              State
              <select
                value={stateFilter}
                onChange={(e) => setStateFilter(e.target.value as NodeState | 'all')}
                className="rounded-lg border border-surface-border bg-surface-900 px-2.5 py-1.5 text-sm text-slate-200 focus:border-brand-500 focus:outline-none"
              >
                <option value="all">All</option>
                {NODE_STATES.map((s) => (
                  <option key={s} value={s}>
                    {humanize(s)}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex items-center gap-2 text-xs text-slate-400">
              Sort by
              <select
                value={sortKey}
                onChange={(e) => setSortKey(e.target.value as SortKey)}
                className="rounded-lg border border-surface-border bg-surface-900 px-2.5 py-1.5 text-sm text-slate-200 focus:border-brand-500 focus:outline-none"
              >
                <option value="node_id">Node ID</option>
                <option value="state">State</option>
                <option value="last_telemetry_timestamp">Last telemetry</option>
              </select>
            </label>
          </div>

          <NodeTable nodes={filtered} />
        </>
      )}
    </div>
  );
}
