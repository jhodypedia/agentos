export type Role = "viewer" | "operator" | "admin" | "owner";

export interface ErrorBody {
  code: string;
  message: string;
  details: Record<string, unknown>;
  request_id?: string | null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const TOKEN_KEY = "agentos.access_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

export function apiBase(): string {
  return (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
}

export function apiUrl(path: string): string {
  const prefix = process.env.NEXT_PUBLIC_API_PREFIX ?? "/api/v1";
  return `${apiBase()}${prefix}${path}`;
}

function extractError(status: number, body: unknown): ApiError {
  const envelope = body as { error?: ErrorBody } | null;
  if (envelope?.error?.message) {
    return new ApiError(status, envelope.error.code, envelope.error.message, envelope.error.details);
  }
  return new ApiError(status, "http_error", `Request failed with status ${status}`);
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit & { auth?: boolean } = {},
): Promise<T> {
  const { auth = true, headers, ...rest } = init;
  const token = auth ? getToken() : null;
  const res = await fetch(apiUrl(path), {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(headers as Record<string, string> | undefined),
    },
    cache: "no-store",
  });

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const body: unknown = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const error = extractError(res.status, body);
    if (res.status === 401 && typeof window !== "undefined") {
      const onLogin = window.location.pathname.startsWith("/login");
      if (!onLogin) {
        setToken(null);
        window.location.replace("/login");
      }
    }
    throw error;
  }
  return body as T;
}

export function login(email: string, password: string): Promise<{ access_token: string }> {
  return apiFetch<{ access_token: string }>("/auth/login", {
    method: "POST",
    auth: false,
    body: JSON.stringify({ email, password }),
  });
}
