"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
}

/**
 * Fetch a JSON endpoint, then keep it fresh.
 *
 * Polling is the floor, not the mechanism: the live updates arrive over SSE
 * (see `useStream`) and call `refresh`. The interval only covers the case
 * where the stream is down, which on a laptop that sleeps is not rare, so it
 * is deliberately slow rather than aggressive.
 */
export function useApi<T>(path: string | null, intervalMs = 30_000): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const alive = useRef(true);

  const load = useCallback(async () => {
    if (path === null) return;
    try {
      const value = await api<T>(path);
      if (!alive.current) return;
      setData(value);
      setError(null);
    } catch (err) {
      if (!alive.current) return;
      setError(err instanceof Error ? err.message : "request failed");
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    alive.current = true;
    void load();
    if (!intervalMs) return () => { alive.current = false; };
    const timer = setInterval(() => void load(), intervalMs);
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
  }, [load, intervalMs]);

  return { data, error, loading, refresh: () => void load() };
}
