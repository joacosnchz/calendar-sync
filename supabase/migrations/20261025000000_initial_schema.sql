-- Calendar Sync schema (equivalent to the former Alembic head 0003, plus the state that
-- used to live in Redis: the per-source access-token cache, per-tenant Graph throttling,
-- the per-source sync lease and pending resync requests).
--
-- Every table is private. The Edge Functions connect to Postgres directly as `postgres`;
-- nothing here is served through the Data API, so RLS is enabled with no policies and the
-- API roles get no privileges.

create table users (
    id uuid primary key default gen_random_uuid(),
    email varchar(320) not null,
    display_name varchar(256) not null default '',
    -- Identity the user signs in to this platform with.
    login_tenant_id varchar(64) not null,
    login_object_id varchar(64) not null,
    is_operator boolean not null default false,
    -- Set when the user asks to delete their account; cleared by deleting the row.
    deletion_requested_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (login_tenant_id, login_object_id)
);
create index ix_users_email on users (email);

create table tenants (
    id uuid primary key default gen_random_uuid(),
    -- Entra ID tenant GUID. Recorded the first time a user links a mailbox from it.
    tenant_id varchar(64) unique,
    display_name varchar(256) not null,
    domain varchar(256),
    -- Per-customer privacy control: when false, its events show as "Busy" with no
    -- subject/location in the unified calendar.
    copy_details boolean not null default true,
    last_error text,
    -- Graph answered 429/503/504 for this tenant: no Graph calls for it until then.
    throttled_until timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table linked_sources (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users (id) on delete cascade,
    tenant_pk uuid not null references tenants (id) on delete cascade,
    source_user_id varchar(64) not null, -- object ID in the source tenant
    source_email varchar(320) not null,
    calendar_id text, -- null = the mailbox's default calendar
    label varchar(128) not null,
    -- active | reauth_required | removing | error
    status varchar(32) not null default 'active',

    -- Delegated access (Calendars.Read + offline_access) granted by the mailbox owner.
    -- Rotated by Entra on use; the newest one is always stored. Encrypted (Fernet).
    refresh_token text,
    granted_scopes text,
    reauth_required_at timestamptz,
    -- Cached delegated access token (encrypted) and when to stop using it.
    access_token text,
    access_token_expires_at timestamptz,

    delta_link text,
    delta_window_start timestamptz,
    delta_window_end timestamptz,
    last_synced_at timestamptz,
    last_full_sync_at timestamptz,
    last_notification_at timestamptz,
    last_error text,
    last_error_at timestamptz,

    -- A delta sync holds this lease so two never run for one source at once.
    sync_lease_until timestamptz,
    -- A sync was asked for but couldn't run then (another one held the lease, or Graph
    -- was throttling/unavailable). The sweep runs it; resync_full asks for a re-baseline.
    resync_requested_at timestamptz,
    resync_full boolean not null default false,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, tenant_pk, source_user_id)
);
create index ix_linked_sources_status on linked_sources (status);
create index ix_linked_sources_tenant_pk on linked_sources (tenant_pk);
create index ix_linked_sources_user_id on linked_sources (user_id);

create table subscriptions (
    id uuid primary key default gen_random_uuid(),
    source_id uuid not null unique references linked_sources (id) on delete cascade,
    graph_subscription_id varchar(128) not null unique,
    client_state text not null, -- encrypted (Fernet)
    resource text not null,
    expires_at timestamptz not null,
    renew_due_at timestamptz not null,
    last_renewed_at timestamptz,
    -- active | renew_failed
    status varchar(32) not null default 'active',
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index ix_subscriptions_renew_due_at on subscriptions (renew_due_at);

-- An event occurrence read from a source calendar. Only the fields the unified calendar
-- shows are kept (never attendees or body). subject and location are encrypted (Fernet).
create table source_events (
    id uuid primary key default gen_random_uuid(),
    source_id uuid not null references linked_sources (id) on delete cascade,
    graph_event_id text not null,
    ical_uid text,
    -- iCalUId when present (same meeting in two sources shows once), else source+event id.
    dedup_key text not null,
    event_type varchar(32) not null default 'singleInstance',
    series_master_id text,
    subject text,
    location text,
    start_utc timestamptz not null,
    end_utc timestamptz not null,
    start_tz varchar(128),
    end_tz varchar(128),
    is_all_day boolean not null default false,
    show_as varchar(32) not null default 'busy',
    sensitivity varchar(32) not null default 'normal',
    last_modified timestamptz,
    updated_at timestamptz not null default now(),
    unique (source_id, graph_event_id)
);
create index ix_source_events_dedup on source_events (dedup_key);
create index ix_source_events_window on source_events (source_id, start_utc, end_utc);

create table sync_errors (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users (id) on delete cascade,
    tenant_pk uuid references tenants (id) on delete cascade,
    source_id uuid references linked_sources (id) on delete set null,
    kind varchar(64) not null,
    message text not null,
    created_at timestamptz not null default now()
);
create index ix_sync_errors_created_at on sync_errors (created_at);
create index ix_sync_errors_source_id on sync_errors (source_id);
create index ix_sync_errors_tenant_pk on sync_errors (tenant_pk);
create index ix_sync_errors_user_id on sync_errors (user_id);

create table audit_log (
    id uuid primary key default gen_random_uuid(),
    actor varchar(320) not null, -- user email, "system" or "graph"
    actor_user_id uuid references users (id) on delete set null,
    action varchar(64) not null,
    target_type varchar(64) not null,
    target_id varchar(128),
    details jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
create index ix_audit_log_action on audit_log (action);
create index ix_audit_log_created_at on audit_log (created_at);

-- updated_at follows every update (SQLAlchemy's onupdate=now() before), unless the
-- update sets it explicitly.
create function set_updated_at() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if new.updated_at is not distinct from old.updated_at then
        new.updated_at = now();
    end if;
    return new;
end;
$$;

create trigger users_updated_at before update on users
    for each row execute function set_updated_at();
create trigger tenants_updated_at before update on tenants
    for each row execute function set_updated_at();
create trigger linked_sources_updated_at before update on linked_sources
    for each row execute function set_updated_at();
create trigger subscriptions_updated_at before update on subscriptions
    for each row execute function set_updated_at();
create trigger source_events_updated_at before update on source_events
    for each row execute function set_updated_at();

-- Private: RLS on with no policies, and no privileges for the Data API roles.
alter table users enable row level security;
alter table tenants enable row level security;
alter table linked_sources enable row level security;
alter table subscriptions enable row level security;
alter table source_events enable row level security;
alter table sync_errors enable row level security;
alter table audit_log enable row level security;

revoke all on users, tenants, linked_sources, subscriptions, source_events, sync_errors, audit_log
    from anon, authenticated;
revoke execute on function set_updated_at() from public, anon, authenticated;
