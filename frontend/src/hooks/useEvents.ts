import { useCallback, useEffect, useState } from 'react';
import { getEvents } from '@/services/api';
import type { FleetEvent, ListEventsQueryOptions } from '@/types/api';

export interface UseEventsState {
  events: FleetEvent[];
  loading: boolean;
  error: Error | undefined;
  /** True once a request has resolved successfully at least once for
   * the current options — mirrors `useNodes`'s own `loaded` flag. */
  loaded: boolean;
  refetch: () => void;
}

/**
 * Fetches the live, bounded fleet-wide event feed from `GET /events`
 * (Step 42). Follows the exact same pattern `useNodes` (Step 40)
 * established: this hook has NO demo-data fallback. A failed call
 * leaves `events` empty and `error` set, so the Events page can show an
 * honest `ErrorState` rather than silently substituting fabricated data.
 *
 * `options` changes (e.g. a filter) trigger a new request automatically
 * — this is the ONE place that decides when to refetch, so
 * `EventsPage`'s filter controls don't need their own fetch logic.
 */
export function useEvents(options?: ListEventsQueryOptions): UseEventsState {
  const [events, setEvents] = useState<FleetEvent[]>([]);
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

    getEvents(options)
      .then((response) => {
        if (cancelled) return;
        setEvents(response.events);
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

  return { events, loading, error, loaded, refetch };
}
