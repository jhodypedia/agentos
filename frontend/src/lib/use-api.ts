"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, apiFetch } from "./api";

interface State<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
  setData: (value: T) => void;
}

/** Fetch a JSON endpoint on mount and on demand; `null` path skips the request. */
export function useApi<T>(path: string | null, deps: unknown[] = []): State<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const [nonce, setNonce] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!path) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    apiFetch<T>(path)
      .then((value) => {
        if (cancelled || !alive.current) return;
        setData(value);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (cancelled || !alive.current) return;
        setError(cause instanceof ApiError ? cause.message : "Request failed");
      })
      .finally(() => {
        if (!cancelled && alive.current) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, nonce, ...deps]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  return { data, error, loading, reload, setData };
}
