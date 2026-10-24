"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, type Me } from "./api";

export interface Resource<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  reload: () => Promise<void>;
}

/**
 * Fetch JSON, optionally polling. Keeps showing the last data while refreshing.
 * A 401 sends the browser to sign in again unless `anonymous` is set (see api()).
 */
export function useResource<T>(
  path: string | null,
  pollMs?: number,
  { anonymous = false }: { anonymous?: boolean } = {},
): Resource<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const [loading, setLoading] = useState(path !== null);
  // Ignore responses for a path we've since moved away from.
  const current = useRef(path);

  const reload = useCallback(async () => {
    if (!path) return;
    try {
      const result = await api<T>(path, { anonymous });
      if (current.current === path) {
        setData(result);
        setError(undefined);
      }
    } catch (err) {
      if (current.current === path) setError(err as Error);
    } finally {
      if (current.current === path) setLoading(false);
    }
  }, [path, anonymous]);

  useEffect(() => {
    current.current = path;
    // Fetching on mount/path change is the point of this hook.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
    if (!pollMs) return;
    const id = setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, pollMs);
    return () => clearInterval(id);
  }, [path, reload, pollMs]);

  return { data, error, loading, reload };
}

/** The signed-in user. Without a session, api() sends the browser to sign in and back. */
export function useMe(options: { requireOperator?: boolean } = {}) {
  const router = useRouter();
  const me = useResource<Me>("/api/me");
  const forbidden = options.requireOperator && me.data && !me.data.is_operator;

  useEffect(() => {
    if (forbidden) router.replace("/calendar");
  }, [forbidden, router]);

  return me;
}
