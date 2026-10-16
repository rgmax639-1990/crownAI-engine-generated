"use client";

import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { api, ApiError, clearToken, getToken } from "./api";

/** Shape of GET /auth/session. */
export type SessionInfo = {
  session_id: string;
  user_id: string;
  user_type: "retail" | "corporate" | "staff" | "prospect";
  display_name: string | null;
  email_masked: string;
  mobile_masked: string;
  staff_roles: string[];
  customer_id: string | null;
  corporate_id: string | null;
  corporate_role: "maker" | "checker" | "authoriser" | "admin" | null;
  scope: "mfa_enrolment" | "mfa_pending" | "full";
  mfa_level: "password_only" | "mfa_complete";
  risk_score: number;
  created_at: string;
  last_activity_at: string;
  expires_at: string;
  idle_timeout_seconds: number;
  warning_seconds: number;
  last_login_at: string | null;
  last_login_ip: string | null;
  preferred_language: string;
  senior_mode: boolean;
  has_strong_factor: boolean;
};

type SessionCtx = {
  session: SessionInfo | null;
  loading: boolean;
  error: ApiError | null;
  refresh: () => Promise<void>;
  logout: (reason?: string) => Promise<void>;
  hasRole: (...roles: string[]) => boolean;
};

const Ctx = createContext<SessionCtx | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setSession(null);
      setLoading(false);
      return;
    }
    try {
      const s = await api<SessionInfo>("/auth/session");
      setSession(s);
      setError(null);
    } catch (e) {
      setSession(null);
      setError(e instanceof ApiError ? e : null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    try {
      if (getToken()) await api("/auth/logout", { method: "POST" });
    } catch {
      /* already ended */
    }
    clearToken();
    setSession(null);
  }, []);

  const hasRole = useCallback((...roles: string[]) => !!session && roles.some((r) => session.staff_roles.includes(r)), [session]);

  return <Ctx.Provider value={{ session, loading, error, refresh, logout, hasRole }}>{children}</Ctx.Provider>;
}

export function useSession(): SessionCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useSession must be used inside SessionProvider (portal or staff layout)");
  return c;
}
