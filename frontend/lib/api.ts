import { useCallback, useEffect, useRef, useState } from "react";

/** Backend base URL. Never hardcode a host anywhere else. */
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000").replace(/\/$/, "");

const TOKEN_KEY = "asr.access_token";
const ONBOARDING_TOKEN_KEY = "asr.onboarding_token";

/** Session-expiry codes from the backend that should send the user back to sign in. */
const SESSION_CODES = new Set(["unauthenticated", "token_invalid", "session_expired", "session_superseded"]);

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  /** Field-level messages (from 422 validation or a custom error with `field`). */
  fieldErrors: Record<string, string>;
  constructor(status: number, code: string, message: string, details: unknown, fieldErrors: Record<string, string>) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.fieldErrors = fieldErrors;
  }
}

// ------------------------------------------------------------------ token storage
function safeStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function getToken(): string | null {
  return safeStorage()?.getItem(TOKEN_KEY) ?? null;
}
export function setToken(token: string) {
  safeStorage()?.setItem(TOKEN_KEY, token);
}
export function clearToken() {
  safeStorage()?.removeItem(TOKEN_KEY);
}
export function getOnboardingToken(): string | null {
  return safeStorage()?.getItem(ONBOARDING_TOKEN_KEY) ?? null;
}
export function setOnboardingToken(token: string) {
  safeStorage()?.setItem(ONBOARDING_TOKEN_KEY, token);
}
export function clearOnboardingToken() {
  safeStorage()?.removeItem(ONBOARDING_TOKEN_KEY);
}

/** Random idempotency key (one per logical submission, reused on retry). */
export function newIdempotencyKey(prefix = "idem"): string {
  const rnd =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${rnd}`;
}

// ------------------------------------------------------------------ fetch
export type ApiOptions = {
  method?: "GET" | "POST" | "PUT" | "DELETE" | "PATCH";
  body?: unknown;
  /** "user" (default) sends the signed-in token, "onboarding" the onboarding token, false none. */
  auth?: "user" | "onboarding" | false;
  signal?: AbortSignal;
  headers?: Record<string, string>;
};

function parseError(status: number, payload: unknown): ApiError {
  const fieldErrors: Record<string, string> = {};
  let code = status === 401 ? "unauthenticated" : status === 403 ? "forbidden" : status === 404 ? "not_found" : "error";
  let message = "Something went wrong. Please try again.";
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (Array.isArray(detail)) {
    code = "validation_error";
    for (const d of detail as Array<{ loc?: unknown[]; msg?: string }>) {
      const loc = Array.isArray(d.loc) ? d.loc.filter((x) => x !== "body") : [];
      const key = String(loc[loc.length - 1] ?? "form");
      const msg = (d.msg || "Invalid value").replace(/^Value error, /, "");
      fieldErrors[key] = /field required|missing/i.test(msg) ? "This field is required" : msg;
    }
    message = Object.keys(fieldErrors).length ? "Please correct the highlighted fields." : message;
  } else if (detail && typeof detail === "object") {
    const d = detail as { code?: string; message?: string; field?: string };
    code = d.code || code;
    message = d.message || message;
    if (d.field) fieldErrors[d.field] = message;
  } else if (typeof detail === "string") {
    message = detail;
  }
  if (status === 0) message = "Cannot reach the bank's servers. Check your connection and try again.";
  if (status >= 500 && !(detail && typeof detail === "object")) message = "The service is temporarily unavailable. Please try again.";
  return new ApiError(status, code, message, payload, fieldErrors);
}

export async function api<T = unknown>(path: string, opts: ApiOptions = {}): Promise<T> {
  const { method = "GET", body, auth = "user", signal, headers = {} } = opts;
  const h: Record<string, string> = { Accept: "application/json", ...headers };
  if (body !== undefined && !(body instanceof FormData)) h["Content-Type"] = "application/json";
  const token = auth === "user" ? getToken() : auth === "onboarding" ? getOnboardingToken() : null;
  if (token) h["Authorization"] = `Bearer ${token}`;
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: h,
      body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
      signal,
      cache: "no-store",
    });
  } catch (e) {
    if ((e as Error)?.name === "AbortError") throw e;
    throw parseError(0, null);
  }
  if (res.status === 204) return undefined as T;
  let payload: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }
  if (!res.ok) {
    const err = parseError(res.status, payload);
    if (res.status === 401 && auth === "user" && SESSION_CODES.has(err.code) && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("auth:expired", { detail: { code: err.code, message: err.message } }));
    }
    throw err;
  }
  if (auth === "user" && typeof window !== "undefined") window.dispatchEvent(new Event("auth:activity"));
  return payload as T;
}

/** Download a file (PDF/CSV) from the API and save it in the browser. */
export async function downloadFile(path: string, fallbackName: string, opts: ApiOptions = {}): Promise<void> {
  const { method = "GET", body, auth = "user" } = opts;
  const h: Record<string, string> = {};
  const token = auth === "user" ? getToken() : auth === "onboarding" ? getOnboardingToken() : null;
  if (token) h["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) h["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw parseError(0, null);
  }
  if (!res.ok) {
    let payload: unknown = null;
    try {
      payload = await res.json();
    } catch {
      /* not JSON */
    }
    throw parseError(res.status, payload);
  }
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") || "";
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  const name = match ? decodeURIComponent(match[1]) : fallbackName;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Upload a file to POST /uploads; returns the upload_ref to put in a later request. */
export async function uploadFile(file: File): Promise<{ upload_ref: string; file_name: string; size_bytes: number; sha256: string }> {
  const fd = new FormData();
  fd.append("file", file);
  return api("/uploads", { method: "POST", body: fd });
}

// ------------------------------------------------------------------ data hook
export type ApiState<T> = {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
  setData: (d: T | null) => void;
};

/**
 * Fetch `path` on mount and whenever it changes. Pass `null` to skip.
 * Returns loading / error / data plus `reload` for retry buttons.
 */
export function useApi<T = unknown>(path: string | null, opts: { auth?: "user" | "onboarding" | false } = {}): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState<boolean>(path !== null);
  const [nonce, setNonce] = useState(0);
  const auth = opts.auth ?? "user";
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (path === null) {
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api<T>(path, { auth, signal: ctrl.signal })
      .then((d) => {
        if (mounted.current) setData(d);
      })
      .catch((e) => {
        if ((e as Error)?.name === "AbortError") return;
        if (mounted.current) setError(e instanceof ApiError ? e : new ApiError(0, "error", String(e), null, {}));
      })
      .finally(() => {
        if (mounted.current && !ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [path, auth, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload, setData };
}
