// 127.0.0.1 (not "localhost") so the browser never tries IPv6 ::1 first on
// machines where localhost resolves there but the backend binds IPv4 only.
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

function friendlyDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const anyDetail = detail as any;
    if (typeof anyDetail.message === "string") return anyDetail.message;
    if (Array.isArray(anyDetail)) {
      return anyDetail
        .map((d) => (d?.msg ? String(d.msg) : JSON.stringify(d)))
        .join(", ");
    }
  }
  return "Something went wrong. Please try again.";
}

export { friendlyDetail };

// Where a 402 (paid feature) response sends the visitor: the API's
// upgrade_url (the Pricing page), tagged so Pricing explains why they're there.
export function upgradeUrlFrom(detail: unknown) {
  const d = detail as { upgrade_url?: string } | string | undefined;
  const raw = typeof d === "object" && d?.upgrade_url ? d.upgrade_url : "/pricing";
  // Only ever follow a same-site path, never an absolute URL from a response.
  const base = raw.startsWith("/") && !raw.startsWith("//") ? raw : "/pricing";
  return `${base}${base.includes("?") ? "&" : "?"}reason=download`;
}

// GETs are idempotent and retried once, so their per-attempt timeout is
// shorter: worst case (7s + 0.75s backoff + 7s) still stays under ~15s.
// Mutations are never retried (a retry could double-submit), so they get
// one long attempt that tolerates a cold or busy backend.
export const GET_TIMEOUT_MS = 7000;
export const MUTATION_TIMEOUT_MS = 15000;
const GET_RETRIES = 1;
const RETRY_BACKOFF_MS = 750;

type RequestOpts = { timeoutMs?: number; retries?: number };

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class RequestTimeout extends Error {}

async function fetchWithTimeout(url: string, options: RequestInit, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (err) {
    if (timedOut) throw new RequestTimeout();
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(
  path: string,
  options: RequestInit,
  token: string | null | undefined,
  { timeoutMs = MUTATION_TIMEOUT_MS, retries = 0 }: RequestOpts
): Promise<T> {
  const headers: Record<string, string> = {
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...(options.headers as Record<string, string> | undefined),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res!: Response;
  for (let attempt = 0; ; attempt++) {
    try {
      res = await fetchWithTimeout(`${API_BASE}${path}`, { ...options, headers }, timeoutMs);
      break;
    } catch (err) {
      // Retries only cover connection-level failures (timeout, server not
      // yet up) -- never 4xx/5xx responses.
      if (attempt < retries) {
        await sleep(RETRY_BACKOFF_MS * (attempt + 1));
        continue;
      }
      if (err instanceof RequestTimeout) {
        throw new ApiError(0, "The server took too long to respond. Please try again in a moment.");
      }
      throw new ApiError(0, "Could not reach the server. Check your connection and try again.");
    }
  }

  if (res.status === 204) return undefined as unknown as T;

  const isJson = res.headers.get("content-type")?.includes("application/json");
  const body = isJson ? await res.json().catch(() => null) : await res.blob();

  if (!res.ok) {
    const detail = isJson ? body?.detail ?? body : body;
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export const api = {
  get: <T>(path: string, token?: string | null, opts: RequestOpts = {}) =>
    request<T>(path, { method: "GET" }, token, { timeoutMs: GET_TIMEOUT_MS, retries: GET_RETRIES, ...opts }),
  post: <T>(path: string, data?: unknown, token?: string | null, opts: RequestOpts = {}) =>
    request<T>(
      path,
      { method: "POST", body: data !== undefined ? JSON.stringify(data) : undefined },
      token,
      { timeoutMs: MUTATION_TIMEOUT_MS, ...opts }
    ),
  del: <T>(path: string, token?: string | null, opts: RequestOpts = {}) =>
    request<T>(path, { method: "DELETE" }, token, { timeoutMs: MUTATION_TIMEOUT_MS, ...opts }),
};

export async function downloadFile(path: string, token: string): Promise<{ blob: Blob; filename: string } | { error: ApiError }> {
  try {
    const res = await fetchWithTimeout(
      `${API_BASE}${path}`,
      { method: "POST", headers: { Authorization: `Bearer ${token}` } },
      MUTATION_TIMEOUT_MS
    );
    if (!res.ok) {
      const isJson = res.headers.get("content-type")?.includes("application/json");
      const body = isJson ? await res.json().catch(() => null) : null;
      return { error: new ApiError(res.status, body?.detail ?? "Download failed.") };
    }
    const disposition = res.headers.get("content-disposition") || "";
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : "crownai_export.zip";
    const blob = await res.blob();
    return { blob, filename };
  } catch (err) {
    if (err instanceof RequestTimeout) {
      return { error: new ApiError(0, "The download took too long. Please try again.") };
    }
    return { error: new ApiError(0, "Could not reach the server.") };
  }
}
