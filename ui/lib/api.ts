/**
 * Browser → worker API. The Mission Control UI is a static export: it owns no database and no
 * secrets, and every request below goes to the SAME origin that served the page (the worker on a
 * Render deployment, or `next dev` proxying to the worker locally). Nothing here computes a number
 * or decides anything — it reads what the worker already decided and writes only through the
 * worker's audited panel endpoints.
 */

export interface AuthStatus {
  password_set: boolean;
  authenticated: boolean;
  session_minutes: number;
}

export interface IntegrationField {
  name: string;
  label: string;
  help: string;
  kind: "text" | "password" | "select" | "bool" | "number";
  options: string[];
  secret: boolean;
  source: "panel" | "env" | "unset";
  fingerprint: string | null;
  updated_at: string | null;
  updated_by: string | null;
  /** `null` for a secret, the stored/effective value otherwise; `"********"` means "a secret is stored". */
  value: string | null;
  effective: unknown;
}

export interface IntegrationGroup {
  key: string;
  title: string;
  subtitle: string;
  test: string;
  fields: IntegrationField[];
}

export interface IntegrationsPayload {
  groups: IntegrationGroup[];
  worker: { version: string; demo: boolean };
}

export interface TestResult {
  ok: boolean;
  detail?: string;
  model?: string;
  endpoint?: string;
  warning?: string | null;
  [key: string]: unknown;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...((init.headers as Record<string, string>) ?? {}) };
  if (init.body) headers["content-type"] = "application/json";
  const res = await fetch(path, { credentials: "include", cache: "no-store", ...init, headers });
  const text = await res.text();
  const body = text ? safeJson(text) : null;
  if (!res.ok) {
    const detail = (body ?? {}) as { message?: string; error?: string };
    throw new ApiError(res.status, detail.message ?? detail.error ?? `HTTP ${res.status}`);
  }
  return body as T;
}

// --------------------------------------------------------------------------- session

export const authStatus = () => request<AuthStatus>("/api/auth/status");

export const authSetup = (password: string) =>
  request<{ ok: boolean }>("/api/auth/setup", { method: "POST", body: JSON.stringify({ password }) });

export const authLogin = (password: string) =>
  request<{ ok: boolean }>("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) });

export const authLogout = () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" });

export const authChangePassword = (current: string, next: string) =>
  request<{ ok: boolean }>("/api/auth/password", {
    method: "POST",
    body: JSON.stringify({ current_password: current, new_password: next }),
  });

// --------------------------------------------------------------------------- settings

export const integrations = () => request<IntegrationsPayload>("/api/admin/integrations");

export const setIntegration = (name: string, value: string) =>
  request<{ ok: boolean }>(`/api/admin/integrations/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify({ value }),
  });

export const clearIntegration = (name: string) =>
  request<{ ok: boolean; cleared: boolean }>(`/api/admin/integrations/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });

export const runConfigSet = (key: string, value: unknown) =>
  request<{ ok: boolean }>("/api/admin/run_config", { method: "POST", body: JSON.stringify({ key, value }) });

export const testGroup = (group: string) =>
  request<TestResult>(`/api/admin/test/${encodeURIComponent(group)}`, { method: "POST" });

// --------------------------------------------------------------------------- bundle

export async function fetchMember<T>(member: string): Promise<T | null> {
  // `/api/bundle/<member>` is built from the database on demand (30s in-process cache) and needs a
  // panel session, so the deployed UI never depends on a static file being regenerated.
  try {
    const res = await fetch(`/api/bundle/${member}`, { cache: "no-store", credentials: "include" });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export const apiMeta = () => request<Record<string, unknown>>("/api/meta");
