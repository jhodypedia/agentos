"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { apiFetch, getToken, setToken } from "./api";
import type { User } from "./types";

type Role = User["role"];
const RANK: Record<Role, number> = { viewer: 10, operator: 20, admin: 30, owner: 40 };

interface AuthState {
  user: User | null;
  loading: boolean;
  error: string | null;
  signIn: (token: string) => Promise<void>;
  signOut: () => void;
  refresh: () => Promise<void>;
  can: (minimum: Role) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      const me = await apiFetch<User>("/auth/me");
      setUser(me);
      setError(null);
    } catch (cause) {
      setUser(null);
      setError(cause instanceof Error ? cause.message : "Unable to load the account");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const signIn = useCallback(
    async (token: string) => {
      setToken(token);
      setLoading(true);
      await refresh();
    },
    [refresh],
  );

  const signOut = useCallback(() => {
    setToken(null);
    setUser(null);
    if (typeof window !== "undefined") window.location.replace("/login");
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      error,
      signIn,
      signOut,
      refresh,
      can: (minimum) => (user ? RANK[user.role] >= RANK[minimum] : false),
    }),
    [user, loading, error, signIn, signOut, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
