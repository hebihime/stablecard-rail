/**
 * The client, and the two hooks every screen reads it through.
 *
 * `useQuery` is deliberately about sixty lines rather than a data-fetching library.
 * What the screens need is small — fetch, refresh, poll, and stop polling when the
 * answer is final — and the interesting behaviour is all in that last clause. A
 * library would supply the first three and still need telling about the fourth.
 */

import type { ReactNode } from 'react';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { resolveConfig } from '../config';
import { StableCardClient } from './client';

const ClientContext = createContext<StableCardClient | null>(null);

export function ApiProvider({
  children,
  client,
}: {
  children: ReactNode;
  /** Injected by tests and by the demo build; resolved from config otherwise. */
  client?: StableCardClient;
}) {
  const resolved = useMemo(
    () => client ?? new StableCardClient({ baseUrl: resolveConfig().apiBaseUrl }),
    [client],
  );
  return <ClientContext.Provider value={resolved}>{children}</ClientContext.Provider>;
}

export function useClient(): StableCardClient {
  const client = useContext(ClientContext);
  if (client === null) {
    throw new Error('useClient must be used inside an ApiProvider');
  }
  return client;
}

export interface Query<T> {
  data: T | null;
  error: unknown;
  /** True only for the first load. A refresh does not blank the screen. */
  loading: boolean;
  refresh: () => void;
}

/**
 * Fetch something, and optionally keep fetching it.
 *
 * Three properties that each fix a specific way a polling screen misbehaves:
 *
 * - **A refresh does not clear `data`.** Otherwise every poll tick flashes the
 *   screen back to a spinner, which on a two-second poll is unusable.
 * - **A late response is discarded.** If the card id changes while a request is in
 *   flight, the old answer must not overwrite the new one — the screen would show
 *   the previous card's balance under the current card's name.
 * - **Polling stops when asked.** `pollWhile` is checked against the newest data,
 *   so a funding intent that reaches a terminal state stops being polled instead
 *   of being asked forever about something that will never change.
 */
export function useQuery<T>(
  fetcher: () => Promise<T>,
  {
    deps,
    intervalMs,
    pollWhile,
    enabled = true,
  }: {
    deps: readonly unknown[];
    intervalMs?: number;
    pollWhile?: (data: T) => boolean;
    enabled?: boolean;
  },
): Query<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(enabled);
  const [nonce, setNonce] = useState(0);

  // Read inside the effect rather than listed as a dependency: they are recreated
  // every render, and depending on them would restart the poll on each tick.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const pollWhileRef = useRef(pollWhile);
  pollWhileRef.current = pollWhile;

  const refresh = useCallback(() => {
    setNonce((current) => current + 1);
  }, []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const run = async () => {
      try {
        const next = await fetcherRef.current();
        if (cancelled) {
          return;
        }
        setData(next);
        setError(null);
        if (intervalMs !== undefined && (pollWhileRef.current?.(next) ?? true)) {
          timer = setTimeout(run, intervalMs);
        }
      } catch (raised) {
        if (cancelled) {
          return;
        }
        setError(raised);
        // Keep polling through a failure: a backend that comes back up should heal
        // the screen without the user doing anything. `data` is left alone, so a
        // transient blip shows a notice beside the last good answer rather than
        // replacing it.
        if (intervalMs !== undefined) {
          timer = setTimeout(run, intervalMs);
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    setLoading((current) => current || data === null);
    void run();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, intervalMs, enabled]);

  return { data, error, loading, refresh };
}
