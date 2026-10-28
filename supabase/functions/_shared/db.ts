import { postgres } from "./deps.ts";

export type Sql = postgres.Sql<Record<string, unknown>>;

/** A connection pool for this function instance. `prepare: false` keeps it compatible
 * with Supavisor's transaction pooler (port 6543) on hosted projects. */
export function createSql(url: string, max = 5): Sql {
  return postgres(url, {
    max,
    prepare: false,
    idle_timeout: 20,
    connect_timeout: 10,
    // Notices (e.g. "relation already exists, skipping") aren't errors.
    onnotice: () => {},
  }) as unknown as Sql;
}

/** Run `fn` in a transaction. postgres.js's TransactionSql type drops the tagged-template
 * call signature, so hand the callback a plain Sql. */
export function transaction<T>(sql: Sql, fn: (tx: Sql) => Promise<T>): Promise<T> {
  return sql.begin((tx) => fn(tx as unknown as Sql)) as Promise<T>;
}

export const SourceStatus = {
  ACTIVE: "active",
  // The delegated grant stopped working (refresh token expired or revoked, MFA or consent
  // now required, password reset...). Syncing is paused and stored events stay in the
  // unified calendar until the user signs in again (Reconnect).
  REAUTH_REQUIRED: "reauth_required",
  REMOVING: "removing",
  ERROR: "error",
} as const;

// Sources whose stored events appear in the unified calendar.
export const VISIBLE_STATUSES = [SourceStatus.ACTIVE, SourceStatus.REAUTH_REQUIRED];

export const SubscriptionStatus = {
  ACTIVE: "active",
  RENEW_FAILED: "renew_failed",
} as const;

// --- row shapes -------------------------------------------------------------------------

export interface UserRow {
  id: string;
  email: string;
  display_name: string;
  login_tenant_id: string;
  login_object_id: string;
  is_operator: boolean;
  deletion_requested_at: Date | null;
  created_at: Date;
  updated_at: Date;
}

export interface TenantRow {
  id: string;
  tenant_id: string | null;
  display_name: string;
  domain: string | null;
  copy_details: boolean;
  last_error: string | null;
  throttled_until: Date | null;
  created_at: Date;
  updated_at: Date;
}

export interface SourceRow {
  id: string;
  user_id: string;
  tenant_pk: string;
  source_user_id: string;
  source_email: string;
  calendar_id: string | null;
  label: string;
  status: string;
  refresh_token: string | null;
  granted_scopes: string | null;
  reauth_required_at: Date | null;
  access_token: string | null;
  access_token_expires_at: Date | null;
  delta_link: string | null;
  delta_window_start: Date | null;
  delta_window_end: Date | null;
  last_synced_at: Date | null;
  last_full_sync_at: Date | null;
  last_notification_at: Date | null;
  last_error: string | null;
  last_error_at: Date | null;
  sync_lease_until: Date | null;
  resync_requested_at: Date | null;
  resync_full: boolean;
  created_at: Date;
  updated_at: Date;
}

/** A source joined with its tenant's Entra tenant id. */
export interface SourceWithTenant extends SourceRow {
  tenant_id: string | null;
}

export interface SubscriptionRow {
  id: string;
  source_id: string;
  graph_subscription_id: string;
  client_state: string;
  resource: string;
  expires_at: Date;
  renew_due_at: Date;
  last_renewed_at: Date | null;
  status: string;
  last_error: string | null;
  created_at: Date;
  updated_at: Date;
}

export interface SourceEventRow {
  id: string;
  source_id: string;
  graph_event_id: string;
  ical_uid: string | null;
  dedup_key: string;
  event_type: string;
  series_master_id: string | null;
  subject: string | null;
  location: string | null;
  start_utc: Date;
  end_utc: Date;
  start_tz: string | null;
  end_tz: string | null;
  is_all_day: boolean;
  show_as: string;
  sensitivity: string;
  last_modified: Date | null;
  updated_at: Date;
}
