import { useEffect, useState } from "react";
import { api } from "./api";

/** Shape of GET /public/environment. */
export type EnvironmentInfo = {
  environment: string;
  all_mock: boolean;
  mock_count: number;
  live_count: number;
  providers: Array<{ provider: string; label: string; mode: string; badge: "MOCK" | "LIVE" }>;
  synthetic_data_only: boolean;
  notice: string | null;
  mock_otp_hint: string | null;
  demo_accounts?: Array<{ role: string; email: string }>;
  demo_password?: string;
};

let cache: Promise<EnvironmentInfo> | null = null;

export function loadEnvironment(force = false): Promise<EnvironmentInfo> {
  if (!cache || force) {
    cache = api<EnvironmentInfo>("/public/environment", { auth: false }).catch((e) => {
      cache = null;
      throw e;
    });
  }
  return cache;
}

/** Which integrations run on mock providers (cached once per page load). */
export function useEnvironment(): { env: EnvironmentInfo | null; error: boolean } {
  const [env, setEnv] = useState<EnvironmentInfo | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let alive = true;
    loadEnvironment()
      .then((e) => alive && setEnv(e))
      .catch(() => alive && setError(true));
    return () => {
      alive = false;
    };
  }, []);
  return { env, error };
}

export function isProviderMock(env: EnvironmentInfo | null, provider: string): boolean {
  if (!env) return true;
  return env.providers.find((p) => p.provider === provider)?.badge !== "LIVE";
}
