// End-user API: profile, linked sources with health (FR-13), unified calendar view.

import { audit } from "../audit.ts";
import { deleteCookie, Hono } from "../deps.ts";
import { SourceStatus, transaction, VISIBLE_STATUSES } from "../db.ts";
import type { Sql } from "../db.ts";
import { busiest } from "../events.ts";
import { SESSION_COOKIE } from "../security.ts";
import type { Services } from "../services.ts";
import { deleteUser, purgeSource } from "../sync/lifecycle.ts";
import { runTask } from "../sync/runner.ts";
import { describeSync, requestResync, SyncMode, syncSource } from "../sync/source_sync.ts";
import { DAY } from "../timeutil.ts";
import { type AppEnv, csrfProtect, currentUser, HttpError, jsonBody, uuidParam } from "./common.ts";

interface SourceJoinRow {
  id: string;
  user_id: string;
  label: string;
  source_email: string;
  status: string;
  tenant_pk: string;
  tenant_display_name: string;
  tenant_id: string | null;
  last_synced_at: Date | null;
  last_full_sync_at: Date | null;
  last_notification_at: Date | null;
  last_error: string | null;
  last_error_at: Date | null;
  reauth_required_at: Date | null;
  created_at: Date;
  sub_id: string | null;
  sub_expires_at: Date | null;
  sub_renew_due_at: Date | null;
  sub_last_renewed_at: Date | null;
  sub_status: string | null;
  sub_last_error: string | null;
  event_count: number;
}

/** Sources with their tenant, subscription health and stored event count. */
export function selectSources(sql: Sql, where: ReturnType<Sql>) {
  return sql<SourceJoinRow[]>`
    select ls.id, ls.user_id, ls.label, ls.source_email, ls.status, ls.tenant_pk,
           t.display_name as tenant_display_name, t.tenant_id,
           ls.last_synced_at, ls.last_full_sync_at, ls.last_notification_at, ls.last_error,
           ls.last_error_at, ls.reauth_required_at, ls.created_at,
           s.id as sub_id, s.expires_at as sub_expires_at, s.renew_due_at as sub_renew_due_at,
           s.last_renewed_at as sub_last_renewed_at, s.status as sub_status, s.last_error as sub_last_error,
           (select count(*)::int from source_events e where e.source_id = ls.id) as event_count
    from linked_sources ls
    join tenants t on t.id = ls.tenant_pk
    left join subscriptions s on s.source_id = ls.id
    where ${where}
    order by ls.created_at
  `;
}

export function sourceOut(row: SourceJoinRow) {
  return {
    id: row.id,
    label: row.label,
    source_email: row.source_email,
    status: row.status,
    tenant: { id: row.tenant_pk, display_name: row.tenant_display_name, tenant_id: row.tenant_id },
    last_synced_at: row.last_synced_at,
    last_full_sync_at: row.last_full_sync_at,
    last_notification_at: row.last_notification_at,
    last_error: row.last_error,
    last_error_at: row.last_error_at,
    subscription: row.sub_id
      ? {
        expires_at: row.sub_expires_at,
        renew_due_at: row.sub_renew_due_at,
        last_renewed_at: row.sub_last_renewed_at,
        status: row.sub_status,
        last_error: row.sub_last_error,
      }
      : null,
    event_count: row.event_count,
    // Where the browser goes to sign in again when reauth_required.
    reauth_url: row.status === SourceStatus.REAUTH_REQUIRED ? `/auth/reauth/${row.id}` : null,
    reauth_required_at: row.reauth_required_at,
    created_at: row.created_at,
  };
}

async function ownedSource(services: Services, userId: string, sourceId: string): Promise<SourceJoinRow> {
  const { sql } = services;
  const [source] = await selectSources(sql, sql`ls.id = ${sourceId}`);
  // Isolation: a user can only ever see or act on their own links.
  if (!source || source.user_id !== userId) throw new HttpError(404, "Source not found");
  return source;
}

/** Run a FULL resync now (in the background), or leave it to the sweep if one is running. */
export function startFullResync(services: Services, sourceId: string): void {
  services.background(
    runTask(services, "sync_source", async () => describeSync(await syncSource(services, sourceId, SyncMode.FULL)), {
      sourceId,
      onTransient: () => requestResync(services.sql, sourceId, true),
    }),
  );
}

export function meRoutes(): Hono<AppEnv> {
  const app = new Hono<AppEnv>();
  app.use(async (c, next) => {
    csrfProtect(c);
    await next();
  });

  app.get("/me", async (c) => {
    const user = await currentUser(c);
    return c.json({ id: user.id, email: user.email, display_name: user.display_name, is_operator: user.is_operator });
  });

  app.delete("/me", async (c) => {
    const user = await currentUser(c);
    const services = c.get("services");
    await transaction(services.sql, async (tx) => {
      await tx`update users set deletion_requested_at = now() where id = ${user.id}`;
      await audit(tx, "user.deletion_requested", "user", user.id, { actor: user });
    });
    // If this is interrupted, the sweep finishes it.
    services.background(runTask(services, "delete_user", () => deleteUser(services, user.id), { userId: user.id }));
    deleteCookie(c, SESSION_COOKIE, { path: "/" });
    return c.json({ status: "deleting" }, 202);
  });

  app.get("/sources", async (c) => {
    const user = await currentUser(c);
    const { sql } = c.get("services");
    const rows = await selectSources(sql, sql`ls.user_id = ${user.id}`);
    return c.json(rows.map(sourceOut));
  });

  app.patch("/sources/:id", async (c) => {
    const user = await currentUser(c);
    const services = c.get("services");
    const source = await ownedSource(services, user.id, uuidParam(c, "id"));
    const body = await jsonBody(c);
    const label = body.label;
    if (typeof label !== "string" || label.length < 1 || label.length > 64) {
      throw new HttpError(422, "label must be a string of 1 to 64 characters");
    }
    await services.sql`update linked_sources set label = ${label} where id = ${source.id}`;
    // Labels are applied when the calendar is read, so there's nothing else to update.
    return c.json(sourceOut({ ...source, label }));
  });

  app.delete("/sources/:id", async (c) => {
    const user = await currentUser(c);
    const services = c.get("services");
    const source = await ownedSource(services, user.id, uuidParam(c, "id"));
    await transaction(services.sql, async (tx) => {
      await tx`update linked_sources set status = ${SourceStatus.REMOVING} where id = ${source.id}`;
      await audit(tx, "source.unlink_requested", "linked_source", source.id, {
        actor: user,
        email: source.source_email,
      });
    });
    // If this is interrupted, the sweep finishes it.
    services.background(
      runTask(services, "remove_source", () => purgeSource(services, source.id, { reason: "unlinked by user" })),
    );
    return c.json({ status: "removing" }, 202);
  });

  app.post("/sources/:id/resync", async (c) => {
    const user = await currentUser(c);
    const services = c.get("services");
    const source = await ownedSource(services, user.id, uuidParam(c, "id"));
    if (source.status !== SourceStatus.ACTIVE) throw new HttpError(409, `Source is ${source.status}`);
    startFullResync(services, source.id);
    return c.json({ status: "queued" }, 202);
  });

  app.get("/calendar", async (c) => {
    // The unified calendar: every visible source's stored events, one entry per meeting
    // (copies in several sources share a dedup key, FR-14).
    const user = await currentUser(c);
    const { sql, cipher } = c.get("services");
    const startRaw = c.req.query("start");
    const endRaw = c.req.query("end");
    if (!startRaw || !endRaw) throw new HttpError(422, "start and end are required");
    const start = new Date(startRaw);
    const end = new Date(endRaw);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
      throw new HttpError(422, "start and end must be ISO 8601 date-times");
    }
    if (end <= start || end.getTime() - start.getTime() > 62 * DAY) {
      throw new HttpError(400, "Range must be positive and at most 62 days");
    }
    const rows = await sql<
      {
        source_id: string;
        dedup_key: string;
        subject: string | null;
        location: string | null;
        start_utc: Date;
        end_utc: Date;
        is_all_day: boolean;
        show_as: string;
        last_modified: Date | null;
        label: string;
        copy_details: boolean;
      }[]
    >`
      select e.source_id, e.dedup_key, e.subject, e.location, e.start_utc, e.end_utc, e.is_all_day,
             e.show_as, e.last_modified, ls.label, t.copy_details
      from source_events e
      join linked_sources ls on ls.id = e.source_id
      join tenants t on t.id = ls.tenant_pk
      where ls.user_id = ${user.id}
        and ls.status in ${sql(VISIBLE_STATUSES)}
        and e.start_utc < ${end}
        and e.end_utc > ${start}
    `;
    const groups = new Map<string, (typeof rows)[number][]>();
    for (const row of rows) {
      const group = groups.get(row.dedup_key);
      if (group) group.push(row);
      else groups.set(row.dedup_key, [row]);
    }

    const out = [];
    for (const [key, members] of groups) {
      let canonical = members[0];
      for (const m of members) {
        if ((m.last_modified ?? m.start_utc) > (canonical.last_modified ?? canonical.start_utc)) canonical = m;
      }
      const copyDetails = members.every((m) => m.copy_details);
      out.push({
        key,
        subject: copyDetails ? (await cipher.decryptOptional(canonical.subject)) ?? "" : "Busy",
        location: copyDetails ? (await cipher.decryptOptional(canonical.location)) ?? "" : "",
        start: canonical.start_utc,
        end: canonical.end_utc,
        is_all_day: canonical.is_all_day,
        show_as: busiest(members.map((m) => m.show_as)),
        labels: [...new Set(members.map((m) => m.label))].sort(),
        source_ids: [...new Set(members.map((m) => m.source_id))].sort(),
      });
    }
    out.sort((a, b) => a.start.getTime() - b.start.getTime() || a.end.getTime() - b.end.getTime());
    return c.json(out);
  });

  return app;
}
