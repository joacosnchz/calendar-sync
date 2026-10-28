// Reading source calendars: fetch-on-notify and calendarView delta queries. Changes are
// written straight to the database by whoever receives them (webhook, sweep, link flow);
// there is no queue in between.

import type { Settings } from "../config.ts";
import { SourceStatus, type SourceWithTenant, type Sql } from "../db.ts";
import { dedupKeyFor, type NormalizedEvent, normalizeEvent, overlaps, type RawEvent, shouldStore } from "../events.ts";
import { GraphGone, GraphNotFound } from "../graph/client.ts";
import type { Services } from "../services.ts";
import { HOUR, syncWindow, utcnow } from "../timeutil.ts";

const DELETE_CHUNK = 500;
const UPSERT_CHUNK = 200;
// Longer than a function instance may run, so a lease only outlives its sync if the
// instance died; the sweep then picks the source up again.
const SYNC_LEASE_MINUTES = 10;

export const SyncMode = {
  INITIAL: "initial", // first backfill after linking
  FULL: "full", // operator- or user-requested re-baseline
  RECOVERY: "recovery", // after `missed` / `subscriptionRemoved` / reconnecting
  SCHEDULED: "scheduled", // periodic safety net
} as const;
export type SyncMode = (typeof SyncMode)[keyof typeof SyncMode];

export function isSyncMode(value: unknown): value is SyncMode {
  return Object.values(SyncMode).includes(value as SyncMode);
}

export interface SyncResult {
  changedKeys: Set<string>;
  rebaseline: boolean;
  pages: number;
  skipped: string | null;
}

export function describeSync(result: SyncResult): string {
  return result.skipped ?? `pages=${result.pages} changed=${result.changedKeys.size} full=${result.rebaseline}`;
}

// --- storage -------------------------------------------------------------------------------

/** Store one event. Returns the dedup keys of the unified-calendar entries that changed.
 *
 * Notification path: only overwrite when Graph's lastModifiedDateTime is newer, which
 * makes duplicate and out-of-order notifications harmless. Delta path is authoritative:
 * any difference wins. */
export function upsertEvent(
  services: Services,
  sourceId: string,
  raw: RawEvent,
  { authoritative }: { authoritative: boolean },
): Promise<Set<string>> {
  return upsertEvents(services, sourceId, [normalizeEvent(raw)], { authoritative });
}

/** Store events in one statement per UPSERT_CHUNK (a delta page is one round trip). */
export async function upsertEvents(
  services: Services,
  sourceId: string,
  events: NormalizedEvent[],
  { authoritative }: { authoritative: boolean },
): Promise<Set<string>> {
  const { sql, cipher } = services;
  const keys = new Set<string>();
  for (let i = 0; i < events.length; i += UPSERT_CHUNK) {
    const rows = await Promise.all(
      events.slice(i, i + UPSERT_CHUNK).map(async (e) => ({
        source_id: sourceId,
        graph_event_id: e.graphEventId,
        ical_uid: e.icalUid,
        dedup_key: dedupKeyFor(sourceId, e),
        event_type: e.eventType,
        series_master_id: e.seriesMasterId,
        subject: await cipher.encrypt(e.subject),
        location: await cipher.encrypt(e.location),
        start_utc: e.startUtc,
        end_utc: e.endUtc,
        start_tz: e.startTz,
        end_tz: e.endTz,
        is_all_day: e.isAllDay,
        show_as: e.showAs,
        sensitivity: e.sensitivity,
        last_modified: e.lastModified,
      })),
    );
    for (const key of await upsertRows(sql, rows, authoritative)) keys.add(key);
  }
  return keys;
}

async function upsertRows(sql: Sql, rows: Record<string, unknown>[], authoritative: boolean): Promise<string[]> {
  const condition = authoritative
    ? sql`source_events.last_modified is distinct from excluded.last_modified
          or source_events.start_utc <> excluded.start_utc
          or source_events.end_utc <> excluded.end_utc`
    : sql`source_events.last_modified is null or source_events.last_modified < excluded.last_modified`;
  const result = await sql<{ dedup_key: string }[]>`
    insert into source_events ${sql(rows as never)}
    on conflict (source_id, graph_event_id) do update set
      ical_uid = excluded.ical_uid,
      dedup_key = excluded.dedup_key,
      event_type = excluded.event_type,
      series_master_id = excluded.series_master_id,
      subject = excluded.subject,
      location = excluded.location,
      start_utc = excluded.start_utc,
      end_utc = excluded.end_utc,
      start_tz = excluded.start_tz,
      end_tz = excluded.end_tz,
      is_all_day = excluded.is_all_day,
      show_as = excluded.show_as,
      sensitivity = excluded.sensitivity,
      last_modified = excluded.last_modified
    where ${condition}
    returning dedup_key
  `;
  return result.map((r) => r.dedup_key);
}

/** Delete events (and occurrences of a deleted series master). Returns affected keys. */
export async function deleteEvents(sql: Sql, sourceId: string, graphIds: string[]): Promise<Set<string>> {
  const keys = new Set<string>();
  for (let i = 0; i < graphIds.length; i += DELETE_CHUNK) {
    const chunk = graphIds.slice(i, i + DELETE_CHUNK);
    const rows = await sql<{ dedup_key: string }[]>`
      delete from source_events
      where source_id = ${sourceId}
        and (graph_event_id in ${sql(chunk)} or series_master_id in ${sql(chunk)})
      returning dedup_key
    `;
    for (const r of rows) keys.add(r.dedup_key);
  }
  return keys;
}

/** Returns [changed keys, needs delta]. Series masters aren't stored directly: their
 * occurrences come from calendarView, so a change to one triggers a delta instead. */
export async function applyEvent(
  services: Services,
  sourceId: string,
  raw: RawEvent,
  window: [Date, Date],
  { authoritative }: { authoritative: boolean },
): Promise<[Set<string>, boolean]> {
  const event = normalizeEvent(raw);
  if (event.eventType === "seriesMaster") return [new Set(), true];
  if (!shouldStore(event) || !overlaps(event, ...window)) {
    return [await deleteEvents(services.sql, sourceId, [event.graphEventId]), false];
  }
  return [await upsertEvent(services, sourceId, raw, { authoritative }), false];
}

export function currentWindow(settings: Settings): [Date, Date] {
  return syncWindow(utcnow(), settings.syncWindowPastDays, settings.syncWindowFutureDays);
}

/** Ask the sweep to sync a source (used when a sync can't run right now). The request is
 * stamped now, so a sync already running doesn't count as having handled it. */
export async function requestResync(sql: Sql, sourceId: string, full = false): Promise<void> {
  await sql`
    update linked_sources
    set resync_requested_at = now(),
        resync_full = resync_full or ${full}
    where id = ${sourceId}
  `;
}

// --- fetch-on-notify -----------------------------------------------------------------------

export async function sourceForSubscription(sql: Sql, graphSubscriptionId: string): Promise<string | null> {
  const [row] = await sql<{ source_id: string }[]>`
    select source_id from subscriptions where graph_subscription_id = ${graphSubscriptionId}
  `;
  return row?.source_id ?? null;
}

export async function processNotification(
  services: Services,
  graphSubscriptionId: string,
  resourceId: string,
): Promise<string> {
  const { sql } = services;
  const [sub] = await sql<{ source_id: string; status: string; source_user_id: string; tenant_id: string | null }[]>`
    update linked_sources ls set last_notification_at = now()
    from subscriptions s, tenants t
    where s.graph_subscription_id = ${graphSubscriptionId} and ls.id = s.source_id and t.id = ls.tenant_pk
    returning ls.id as source_id, ls.status, ls.source_user_id, t.tenant_id
  `;
  if (!sub) return "unknown-subscription";
  if (sub.status !== SourceStatus.ACTIVE || !sub.tenant_id) return "inactive";

  const graph = services.graph.source(sub.source_id, sub.tenant_id);
  let keys: Set<string>;
  let needsDelta = false;
  try {
    const raw = await graph.getEvent(sub.source_user_id, resourceId);
    [keys, needsDelta] = await applyEvent(services, sub.source_id, raw, currentWindow(services.settings), {
      authoritative: false,
    });
  } catch (err) {
    if (!(err instanceof GraphNotFound)) throw err;
    keys = await deleteEvents(sql, sub.source_id, [resourceId]);
    // Unknown id: most likely a series master (we store occurrences). Let delta work out
    // which occurrences went away.
    needsDelta = keys.size === 0;
  }
  if (needsDelta) await syncSource(services, sub.source_id, SyncMode.RECOVERY);
  return `changed=${keys.size} delta=${needsDelta}`;
}

// --- delta ---------------------------------------------------------------------------------

// lease_started_at stays text end to end (postgres.js turns timestamptz values, even
// strings, into JS Dates, which would drop now()'s microseconds and break the comparison
// in releaseSource).
type LeasedSource = SourceWithTenant & { lease_started_at: string };

/** Take the source's sync lease. Null when the source isn't syncable or another sync holds
 * the lease.
 *
 * The run also records itself as a resync request (unless an older one is pending), which
 * is cleared when it ends: if the instance dies mid-sync, the request outlives the lease
 * and the sweep runs the sync again. A request made while the sync runs is newer than its
 * start, so it survives the run. */
async function leaseSource(sql: Sql, sourceId: string, full: boolean): Promise<LeasedSource | null> {
  const [source] = await sql<LeasedSource[]>`
    update linked_sources ls
    set sync_lease_until = now() + make_interval(mins => ${SYNC_LEASE_MINUTES}),
        resync_requested_at = coalesce(ls.resync_requested_at, now()),
        resync_full = ls.resync_full or ${full}
    from tenants t
    where ls.id = ${sourceId} and t.id = ls.tenant_pk
      and ls.status = ${SourceStatus.ACTIVE} and t.tenant_id is not null
      and (ls.sync_lease_until is null or ls.sync_lease_until < now())
    returning ls.*, t.tenant_id, now()::text as lease_started_at
  `;
  return source ?? null;
}

/** Release the lease and the requests this run covered (it failed or succeeded; a failure
 * is recorded by the task runner, which asks again if it was transient). */
async function releaseSource(sql: Sql, sourceId: string, startedAt: string): Promise<void> {
  await sql`
    update linked_sources set
      sync_lease_until = null,
      resync_requested_at = case when resync_requested_at <= ${startedAt}::text::timestamptz then null
                                 else resync_requested_at end,
      resync_full = case when resync_requested_at <= ${startedAt}::text::timestamptz then false else resync_full end
    where id = ${sourceId}
  `;
}

export async function syncSource(services: Services, sourceId: string, mode: SyncMode): Promise<SyncResult> {
  const { sql, settings } = services;
  const source = await leaseSource(sql, sourceId, mode === SyncMode.INITIAL || mode === SyncMode.FULL);
  if (!source) {
    const [row] = await sql<{ status: string; tenant_id: string | null }[]>`
      select ls.status, t.tenant_id from linked_sources ls join tenants t on t.id = ls.tenant_pk
      where ls.id = ${sourceId}
    `;
    if (!row || row.status !== SourceStatus.ACTIVE || !row.tenant_id) {
      return { changedKeys: new Set(), rebaseline: false, pages: 0, skipped: "inactive" };
    }
    // Another sync is running: make sure the sweep runs one more after it.
    await requestResync(sql, sourceId, mode === SyncMode.INITIAL || mode === SyncMode.FULL);
    return { changedKeys: new Set(), rebaseline: false, pages: 0, skipped: "busy" };
  }

  try {
    const now = utcnow();
    const rebaseline = mode === SyncMode.INITIAL ||
      mode === SyncMode.FULL ||
      source.resync_full ||
      !source.delta_link ||
      source.last_full_sync_at === null ||
      source.last_full_sync_at.getTime() < now.getTime() - settings.deltaRebaselineHours * HOUR;
    let result: SyncResult;
    try {
      result = await runDelta(services, source, rebaseline);
    } catch (err) {
      if (!(err instanceof GraphGone) || rebaseline) throw err;
      console.info(`delta token expired for source ${source.id}; re-baselining`);
      result = await runDelta(services, source, true);
    }
    if ((mode === SyncMode.RECOVERY || mode === SyncMode.SCHEDULED) && result.changedKeys.size) {
      // Changes a recovery/scheduled delta found that notifications had not delivered.
      console.warn(
        `delta drift tenant=${source.tenant_id} source=${source.id} mode=${mode} changes=${result.changedKeys.size}`,
      );
    }
    return result;
  } finally {
    await releaseSource(sql, sourceId, source.lease_started_at);
  }
}

async function runDelta(services: Services, source: SourceWithTenant, rebaseline: boolean): Promise<SyncResult> {
  const { sql } = services;
  const graph = services.graph.source(source.id, source.tenant_id!);
  const result: SyncResult = { changedKeys: new Set(), rebaseline, pages: 0, skipped: null };
  let window: [Date, Date];
  let url: string | null;
  if (rebaseline) {
    window = currentWindow(services.settings);
    url = graph.deltaStartUrl(source.source_user_id, source.calendar_id, ...window);
  } else {
    window = [source.delta_window_start!, source.delta_window_end!];
    url = source.delta_link;
  }

  const seen = new Set<string>();
  let deltaLink: string | null = null;
  while (url) {
    const page = await graph.deltaPage(url);
    result.pages += 1;
    // The last entry for an id wins; then one delete and one upsert for the whole page.
    const latest = new Map<string, RawEvent>();
    for (const item of page.value ?? []) latest.set(item.id, item);
    const gone: string[] = [];
    const store: NormalizedEvent[] = [];
    for (const [id, item] of latest) {
      if ("@removed" in item) {
        gone.push(id);
        continue;
      }
      seen.add(id);
      const event = normalizeEvent(item);
      // Series masters aren't stored: delta returns their occurrences.
      if (event.eventType === "seriesMaster") continue;
      if (shouldStore(event) && overlaps(event, ...window)) store.push(event);
      else gone.push(id);
    }
    for (const k of await deleteEvents(sql, source.id, gone)) result.changedKeys.add(k);
    for (const k of await upsertEvents(services, source.id, store, { authoritative: true })) result.changedKeys.add(k);
    url = page["@odata.nextLink"] ?? null;
    deltaLink = page["@odata.deltaLink"] ?? deltaLink;
  }

  if (rebaseline) {
    // Delta is the source of truth: anything stored that a full pass didn't return is gone.
    const existing = await sql<{ graph_event_id: string }[]>`
      select graph_event_id from source_events where source_id = ${source.id}
    `;
    const gone = existing.map((r) => r.graph_event_id).filter((id) => !seen.has(id)).sort();
    for (const k of await deleteEvents(sql, source.id, gone)) result.changedKeys.add(k);
  }

  await sql`
    update linked_sources set
      delta_link = ${deltaLink},
      delta_window_start = ${window[0]},
      delta_window_end = ${window[1]},
      last_synced_at = now(),
      last_full_sync_at = case when ${rebaseline} then now() else last_full_sync_at end,
      last_error = null
    where id = ${source.id}
  `;
  return result;
}
