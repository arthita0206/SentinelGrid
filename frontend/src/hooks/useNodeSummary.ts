import { useCallback, useEffect, useState } from 'react';
import { ApiError, getNodeSummary } from '@/services/api';
import type { NodeSummaryResponse } from '@/types/api';

export interface UseNodeSummaryState {
  summary: NodeSummaryResponse | undefined;
  loading: boolean;
  error: Error | undefined;
  /** True specifically when the live call failed with a 404
   * `NODE_NOT_FOUND` — lets the page show an honest "Node not found"
   * state instead of a generic error, and instead of ANY fabricated
   * node data (see this hook's own module docstring for why there is no
   * mock fallback here anymore, Step 44). */
  notFound: boolean;
  loaded: boolean;
  refetch: () => void;
}

/**
 * Fetches the combined node summary (current state + anomaly/recovery
 * history) for `nodeId` from `GET /nodes/{node_id}/summary`.
 *
 * **Step 44 change — no more mock-data fallback.** Through Step 43 this
 * hook (then built on `useApiResource`) silently fell back to fabricated
 * demo data on ANY failure, including a genuine "this node does not
 * exist" 404 — the fabricated Node Identity/Current Fault/history cards
 * looked exactly like a successful load, with only a small tooltip
 * reason hinting otherwise. That is exactly the "silently fall back to
 * demo data on API failure" pattern every other page in this app
 * (`useNodes`, `useEvents`, `useMetrics`) deliberately does NOT do, and
 * it made a missing node indistinguishable from a real one at a glance.
 * This hook now follows that same no-fallback pattern: a failure leaves
 * `summary` undefined and `error` (or `notFound`) set, so
 * `NodeDetailPage` can show an honest "Node not found" or `ErrorState`
 * instead. `useApiResource.ts`/`src/data/mockEvents.ts` (the old
 * fallback machinery this hook used) were removed as part of this
 * change since nothing else used them.
 */
export function useNodeSummary(nodeId: string): UseNodeSummaryState {
  const [summary, setSummary] = useState<NodeSummaryResponse | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [notFound, setNotFound] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [refetchToken, setRefetchToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(undefined);
    setNotFound(false);

    getNodeSummary(nodeId, { limit: 20 })
      .then((response) => {
        if (cancelled) return;
        setSummary(response);
        setLoaded(true);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
        } else {
          setError(err instanceof Error ? err : new Error(String(err)));
        }
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refetch is keyed by nodeId/refetchToken, not fetcher identity
  }, [nodeId, refetchToken]);

  const refetch = useCallback(() => setRefetchToken((t) => t + 1), []);

  return { summary, loading, error, notFound, loaded, refetch };
}
