import { useCallback, useEffect, useState } from 'react';
import { getNodes } from '@/services/api';
import type { ListNodesQueryOptions, NodeCurrentState } from '@/types/api';

export interface UseNodesState {
  nodes: NodeCurrentState[];
  loading: boolean;
  error: Error | undefined;
  /** True once a request has resolved successfully at least once for
   * the current options — distinct from `!loading && !error`, so a
   * consumer can render "loading" vs. "loaded but empty" unambiguously. */
  loaded: boolean;
  refetch: () => void;
}

/**
 * Fetches the live node fleet from `GET /nodes` (Step 40).
 *
 * Deliberately does NOT fall back to demo data on failure — the Nodes
 * page must show an honest error state instead (Step 40 Phase 9: "Do NOT
 * silently fall back to demo data on a successful API response. If the
 * API is unavailable, show an honest error state."). This hook has no
 * mock fallback path at all — a failed call simply leaves `nodes` empty
 * and `error` set. As of Step 44, every data-fetching hook in this
 * directory follows this same no-fallback pattern (`useEvents`,
 * `useMetrics`, `useNodeSummary`) — there is no mock-fallback hook left
 * anywhere in this app.
 */
export function useNodes(options?: ListNodesQueryOptions): UseNodesState {
  const [nodes, setNodes] = useState<NodeCurrentState[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [loaded, setLoaded] = useState(false);
  const [refetchToken, setRefetchToken] = useState(0);

  // Stable, order-independent key so an options object literal passed
  // fresh on every render doesn't cause an infinite refetch loop.
  const optionsKey = JSON.stringify(options ?? {});

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(undefined);

    getNodes(options)
      .then((response) => {
        if (cancelled) return;
        setNodes(response.nodes);
        setLoaded(true);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err : new Error(String(err)));
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refetch is keyed by optionsKey/refetchToken, not object identity
  }, [optionsKey, refetchToken]);

  const refetch = useCallback(() => setRefetchToken((t) => t + 1), []);

  return { nodes, loading, error, loaded, refetch };
}
