import { useCallback, useEffect, useRef, useState } from 'react';
import { toApiError } from '../api/client.js';

/**
 * Poll an async fetcher on an interval.
 *
 * Deliberate behaviour:
 *  - the last good payload is KEPT when a poll fails, so a backend that dies
 *    mid-session degrades to "stale data + error banner" instead of a blank UI;
 *  - overlapping requests are skipped rather than queued;
 *  - `fetcher` must be stable (wrap it in useCallback) or the interval resets.
 *
 * @param {() => Promise<any>} fetcher
 * @param {number} intervalMs  poll period; <= 0 disables polling (fetch once)
 * @param {{ enabled?: boolean }} options
 */
export function usePolling(fetcher, intervalMs, { enabled = true } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState(null);

  const inFlight = useRef(false);
  const alive = useRef(true);

  const run = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const result = await fetcher();
      if (!alive.current) return;
      setData(result);
      setError(null);
      setLastUpdated(new Date());
    } catch (err) {
      if (!alive.current) return;
      setError(toApiError(err));
    } finally {
      inFlight.current = false;
      if (alive.current) setLoading(false);
    }
  }, [fetcher]);

  useEffect(() => {
    alive.current = true;
    if (!enabled) {
      setLoading(false);
      return () => {
        alive.current = false;
      };
    }

    setLoading(true);
    run();

    if (intervalMs <= 0) {
      return () => {
        alive.current = false;
      };
    }

    const id = window.setInterval(run, intervalMs);
    return () => {
      alive.current = false;
      window.clearInterval(id);
    };
  }, [run, intervalMs, enabled]);

  return { data, error, loading, lastUpdated, refresh: run };
}
