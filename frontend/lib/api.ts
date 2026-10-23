// Typed client for the FastAPI backend (proxied same-origin under /api and /auth).

export type PrimaryStatus = "unassigned" | "provisioning" | "ready" | "error";

export interface Me {
  id: string;
  email: string;
  display_name: string;
  is_operator: boolean;
  primary: { status: PrimaryStatus; mailbox: string | null; error: string | null; mode: string };
}

export interface Source {
  id: string;
  label: string;
  source_email: string;
  status: "pending_consent" | "active" | "removing" | "revoked" | "error";
  tenant: { id: string; display_name: string; tenant_id: string | null; consent_status: string };
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
  consent_url: string | null;
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
  mirrored: boolean;
}

export interface Tenant {
  id: string;
  display_name: string;
  domain: string | null;
  tenant_id: string | null;
  consent_status: "pending" | "connected" | "denied" | "revoked";
  copy_details: boolean;
  connected_at: string | null;
  revoked_at: string | null;
  last_error: string | null;
  created_at: string;
  source_count: number;
  active_source_count: number;
  next_subscription_expiry: string | null;
  last_notification_at: string | null;
  errors_24h: number;
  consent_url: string | null;
}

export interface AdminUser {
  id: string;
  email: string;
  display_name: string;
  is_operator: boolean;
  primary_status: PrimaryStatus;
  primary_mailbox: string | null;
  primary_error: string | null;
  source_count: number;
  mirror_count: number;
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
  tenants: Record<string, number>;
  sources: Record<string, number>;
  subscriptions: number;
  subscriptions_expiring_24h: number;
  subscriptions_failing: number;
  errors_24h: number;
  mirrors: number;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  // Required by the backend on mutating requests (CSRF defence alongside SameSite cookies).
  headers.set("x-calsync-csrf", "1");
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  const res = await fetch(path, { ...init, headers, credentials: "same-origin", cache: "no-store" });
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
