import { useCallback, useEffect, useState } from 'react';
import { getMetrics } from '@/services/api';
import type { GetMetricsQueryOptions, MetricsResponse } from '@/types/api';

export interface UseMetricsState {
  metrics: MetricsResponse | undefined;
  loading: boolean;
  error: Error | undefined;
  /** True once a request has resolved successfully at least once for
   * the current options — mirrors `useNodes`'s/`useEvents`'s own
   * `loaded` flag. */
  loaded: boolean;
  refetch: () => void;
}

/**
 * Fetches the live, bounded CloudWatch metrics feed from `GET /metrics`
 * (Step 43). Follows the exact same pattern `useNodes` (Step 40) and
 * `useEvents` (Step 42) established: this hook has NO demo-data
 * fallback. A failed call leaves `metrics` undefined and `error` set, so
 * the Analytics page can show an honest `ErrorState` rather than
 * silently substituting synthetic series.
 *
 * `options` changes (e.g. a time-range/subsystem/fault-type filter)
 * trigger a new request automatically — this is the ONE place that
 * decides when to refetch, so `AnalyticsPage`'s filter controls don't
 * need their own fetch logic.
 */
export function useMetrics(options?: GetMetricsQueryOptions): UseMetricsState {
  const [metrics, setMetrics] = useState<MetricsResponse | undefined>(undefined);
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

    getMetrics(options)
      .then((response) => {
        if (cancelled) return;
        setMetrics(response);
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

  return { metrics, loading, error, loaded, refetch };
}
