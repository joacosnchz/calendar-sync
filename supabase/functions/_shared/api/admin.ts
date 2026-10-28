// Operator API: customer tenants (FR-1), users, sync health (FR-13).
//
// Customer tenants are recorded automatically the first time a user links a mailbox from
// one; there is no onboarding or admin consent step. Operators can rename them and set the
// per-tenant privacy control.

import { audit } from "../audit.ts";
import { Hono } from "../deps.ts";
import { SourceStatus, type Sql, SubscriptionStatus, type TenantRow, transaction } from "../db.ts";
import { isUuid } from "../security.ts";
import {
  type AppEnv,
  csrfProtect,
  HttpError,
  intQuery,
  jsonBody,
  optionalBool,
  optionalString,
  requireOperator,
  uuidParam,
} from "./common.ts";
import { startFullResync } from "./me.ts";

async function tenantOut(sql: Sql, tenants: TenantRow[]) {
  if (!tenants.length) return [];
  const ids = tenants.map((t) => t.id);
  const stats = await sql<
    {
      tenant_pk: string;
      total: number;
      active: number;
      reauth_required: number;
      last_notification: Date | null;
      next_expiry: Date | null;
    }[]
  >`
    select ls.tenant_pk,
           count(*)::int as total,
           (count(*) filter (where ls.status = ${SourceStatus.ACTIVE}))::int as active,
           (count(*) filter (where ls.status = ${SourceStatus.REAUTH_REQUIRED}))::int as reauth_required,
           max(ls.last_notification_at) as last_notification,
           min(s.expires_at) as next_expiry
    from linked_sources ls
    left join subscriptions s on s.source_id = ls.id
    where ls.tenant_pk in ${sql(ids)}
    group by ls.tenant_pk
  `;
  const errors = await sql<{ tenant_pk: string; n: number }[]>`
    select tenant_pk, count(*)::int as n from sync_errors
    where tenant_pk in ${sql(ids)} and created_at > now() - interval '24 hours'
    group by tenant_pk
  `;
  const byTenant = new Map(stats.map((s) => [s.tenant_pk, s]));
  const errorsByTenant = new Map(errors.map((e) => [e.tenant_pk, e.n]));
  return tenants.map((t) => {
    const s = byTenant.get(t.id);
    return {
      id: t.id,
      display_name: t.display_name,
      domain: t.domain,
      tenant_id: t.tenant_id,
      copy_details: t.copy_details,
      last_error: t.last_error,
      created_at: t.created_at,
      source_count: s?.total ?? 0,
      active_source_count: s?.active ?? 0,
      reauth_required_count: s?.reauth_required ?? 0,
      next_subscription_expiry: s?.next_expiry ?? null,
      last_notification_at: s?.last_notification ?? null,
      errors_24h: errorsByTenant.get(t.id) ?? 0,
    };
  });
}

export function adminRoutes(): Hono<AppEnv> {
  const app = new Hono<AppEnv>();
  app.use(async (c, next) => {
    csrfProtect(c);
    await next();
  });

  app.get("/overview", async (c) => {
    await requireOperator(c);
    const { sql } = c.get("services");
    const sources = await sql<{ status: string; n: number }[]>`
      select status, count(*)::int as n from linked_sources group by status
    `;
    const [row] = await sql<
      {
        users: number;
        tenants: number;
        subscriptions: number;
        subscriptions_expiring_24h: number;
        subscriptions_failing: number;
        errors_24h: number;
        events: number;
      }[]
    >`
      select
        (select count(*)::int from users) as users,
        (select count(*)::int from tenants) as tenants,
        (select count(*)::int from subscriptions) as subscriptions,
        (select count(*)::int from subscriptions where expires_at < now() + interval '24 hours')
          as subscriptions_expiring_24h,
        (select count(*)::int from subscriptions where status <> ${SubscriptionStatus.ACTIVE})
          as subscriptions_failing,
        (select count(*)::int from sync_errors where created_at > now() - interval '24 hours') as errors_24h,
        (select count(*)::int from source_events) as events
    `;
    return c.json({
      users: row.users,
      tenants: row.tenants,
      sources: Object.fromEntries(sources.map((s) => [s.status, s.n])),
      subscriptions: row.subscriptions,
      subscriptions_expiring_24h: row.subscriptions_expiring_24h,
      subscriptions_failing: row.subscriptions_failing,
      errors_24h: row.errors_24h,
      events: row.events,
    });
  });

  // --- tenants -----------------------------------------------------------------------------

  app.get("/tenants", async (c) => {
    await requireOperator(c);
    const { sql } = c.get("services");
    const tenants = await sql<TenantRow[]>`select * from tenants order by display_name`;
    return c.json(await tenantOut(sql, tenants));
  });

  app.patch("/tenants/:id", async (c) => {
    const operator = await requireOperator(c);
    const { sql } = c.get("services");
    const tenantPk = uuidParam(c, "id");
    const body = await jsonBody(c);
    const displayName = optionalString(body, "display_name", 1, 128);
    const copyDetails = optionalBool(body, "copy_details");
    const tenant = await transaction(sql, async (tx) => {
      const [tenant] = await tx<TenantRow[]>`
        update tenants set
          display_name = coalesce(${displayName ?? null}, display_name),
          copy_details = coalesce(${copyDetails ?? null}, copy_details)
        where id = ${tenantPk}
        returning *
      `;
      if (!tenant) throw new HttpError(404, "Tenant not found");
      const changes: Record<string, unknown> = {};
      if (displayName !== undefined) changes.display_name = displayName;
      if (copyDetails !== undefined) changes.copy_details = copyDetails;
      await audit(tx, "tenant.updated", "tenant", tenant.id, { actor: operator, ...changes });
      return tenant;
    });
    // The privacy control is applied when the calendar is read: it takes effect at once.
    return c.json((await tenantOut(sql, [tenant]))[0]);
  });

  // --- users -------------------------------------------------------------------------------

  app.get("/users", async (c) => {
    await requireOperator(c);
    const { sql } = c.get("services");
    const users = await sql`
      select u.id, u.email, u.display_name, u.is_operator, u.created_at,
             (select count(*)::int from linked_sources ls where ls.user_id = u.id) as source_count,
             (select count(e.id)::int from linked_sources ls join source_events e on e.source_id = ls.id
               where ls.user_id = u.id) as event_count
      from users u
      order by u.created_at
    `;
    return c.json(
      users.map((u) => ({
        id: u.id,
        email: u.email,
        display_name: u.display_name,
        is_operator: u.is_operator,
        source_count: u.source_count,
        event_count: u.event_count,
        created_at: u.created_at,
      })),
    );
  });

  app.patch("/users/:id", async (c) => {
    const operator = await requireOperator(c);
    const { sql } = c.get("services");
    const userId = uuidParam(c, "id");
    const body = await jsonBody(c);
    const isOperator = optionalBool(body, "is_operator");
    await transaction(sql, async (tx) => {
      const [user] = await tx<{ id: string }[]>`select id from users where id = ${userId} for update`;
      if (!user) throw new HttpError(404, "User not found");
      const changes: Record<string, unknown> = {};
      if (isOperator !== undefined) {
        if (user.id === operator.id && !isOperator) {
          throw new HttpError(400, "You can't remove your own operator access");
        }
        await tx`update users set is_operator = ${isOperator} where id = ${userId}`;
        changes.is_operator = isOperator;
      }
      await audit(tx, "user.updated", "user", userId, { actor: operator, ...changes });
    });
    return c.json({ status: "updated" }, 202);
  });

  app.post("/sources/:id/resync", async (c) => {
    const operator = await requireOperator(c);
    const services = c.get("services");
    const sourceId = uuidParam(c, "id");
    const [source] = await services.sql<{ id: string }[]>`select id from linked_sources where id = ${sourceId}`;
    if (!source) throw new HttpError(404, "Source not found");
    await audit(services.sql, "source.resync_requested", "linked_source", source.id, { actor: operator });
    startFullResync(services, source.id);
    return c.json({ status: "queued" }, 202);
  });

  // --- health ------------------------------------------------------------------------------

  app.get("/subscriptions", async (c) => {
    await requireOperator(c);
    const { sql } = c.get("services");
    const rows = await sql`
      select s.id, s.graph_subscription_id, s.source_id, ls.source_email, u.email as user_email,
             t.display_name as tenant_name, s.expires_at, s.renew_due_at, s.last_renewed_at, s.status,
             s.last_error
      from subscriptions s
      join linked_sources ls on ls.id = s.source_id
      join users u on u.id = ls.user_id
      join tenants t on t.id = ls.tenant_pk
      order by s.expires_at
    `;
    return c.json(rows);
  });

  app.get("/errors", async (c) => {
    await requireOperator(c);
    const { sql } = c.get("services");
    const limit = intQuery(c, "limit", 100, 500);
    const tenantPk = c.req.query("tenant_pk");
    if (tenantPk !== undefined && !isUuid(tenantPk)) throw new HttpError(422, "tenant_pk must be a UUID");
    const rows = await sql`
      select e.id, e.kind, e.message, e.created_at, ls.source_email, u.email as user_email,
             t.display_name as tenant_name
      from sync_errors e
      left join linked_sources ls on ls.id = e.source_id
      left join users u on u.id = e.user_id
      left join tenants t on t.id = e.tenant_pk
      where ${tenantPk ? sql`e.tenant_pk = ${tenantPk}` : sql`true`}
      order by e.created_at desc
      limit ${limit}
    `;
    return c.json(rows);
  });

  app.get("/audit", async (c) => {
    await requireOperator(c);
    const { sql } = c.get("services");
    const limit = intQuery(c, "limit", 100, 500);
    const rows = await sql`
      select id, actor, action, target_type, target_id, details, created_at
      from audit_log order by created_at desc limit ${limit}
    `;
    return c.json(rows);
  });

  return app;
}
