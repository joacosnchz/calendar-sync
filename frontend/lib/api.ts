// Typed client for the FastAPI backend (proxied same-origin under /api and /auth).

export interface Me {
  id: string;
  email: string;
  display_name: string;
  is_operator: boolean;
}

export interface Source {
  id: string;
  label: string;
  source_email: string;
  // reauth_required: the refresh token expired or was revoked. Events already synced are kept
  // (and still shown), but syncing is paused until the user signs in again via reauth_url.
  status: "active" | "reauth_required" | "removing" | "error";
  tenant: { id: string; display_name: string; tenant_id: string | null };
  last_synced_at: string | null;
  last_full_sync_at: string | null;
  last_notification_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  subscription: {
    expires_at: string;
    renew_due_at: string;
    last_renewed_at: string | null;
    status: string;
    last_error: string | null;
  } | null;
  event_count: number;
  reauth_url: string | null;
  reauth_required_at: string | null;
  created_at: string;
}

export interface CalendarEvent {
  key: string;
  subject: string;
  location: string;
  start: string;
  end: string;
  is_all_day: boolean;
  show_as: string;
  labels: string[];
  source_ids: string[];
}

export interface Tenant {
  id: string;
  display_name: string;
  domain: string | null;
  tenant_id: string | null;
  copy_details: boolean;
  last_error: string | null;
  created_at: string;
  source_count: number;
  active_source_count: number;
  reauth_required_count: number;
  next_subscription_expiry: string | null;
  last_notification_at: string | null;
  errors_24h: number;
}

export interface AdminUser {
  id: string;
  email: string;
  display_name: string;
  is_operator: boolean;
  source_count: number;
  event_count: number;
  created_at: string;
}

export interface AdminSubscription {
  id: string;
  graph_subscription_id: string;
  source_id: string;
  source_email: string;
  user_email: string;
  tenant_name: string;
  expires_at: string;
  renew_due_at: string;
  last_renewed_at: string | null;
  status: string;
  last_error: string | null;
}

export interface SyncErrorRow {
  id: string;
  kind: string;
  message: string;
  created_at: string;
  source_email: string | null;
  user_email: string | null;
  tenant_name: string | null;
}

export interface AuditRow {
  id: string;
  actor: string;
  action: string;
  target_type: string;
  target_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface Overview {
  users: number;
  tenants: number;
  sources: Record<string, number>;
  subscriptions: number;
  subscriptions_expiring_24h: number;
  subscriptions_failing: number;
  errors_24h: number;
  events: number;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// Set once we start a full-page navigation away (sign-in or sign-out), so late 401s from
// requests still in flight don't trigger a second redirect.
let leaving = false;

/** Full-page navigation (OAuth flows, sign-out). Later 401s no longer redirect. */
export function navigate(href: string) {
  leaving = true;
  window.location.assign(href);
}

/** The platform session expired: sign in again and come back to this page. */
export function signInAgain() {
  if (leaving) return;
  navigate(`/auth/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
}

export interface ApiInit extends RequestInit {
  /** Treat 401 as an ordinary error instead of redirecting to sign-in (e.g. the landing page). */
  anonymous?: boolean;
}

export async function api<T>(path: string, { anonymous, ...init }: ApiInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  // Required by the backend on mutating requests (CSRF defence alongside SameSite cookies).
  headers.set("x-calsync-csrf", "1");
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  const res = await fetch(path, { ...init, headers, credentials: "same-origin", cache: "no-store" });
  if (res.status === 401 && !anonymous) {
    signInAgain();
    // Never settles: the page is navigating away, so don't flash an error in the meantime.
    return new Promise<T>(() => {});
  }
  if (!res.ok) {
    let message = res.statusText;
    try {
      const body = await res.json();
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const patch = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });
