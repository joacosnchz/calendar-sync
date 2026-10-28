// The periodic sweep, run by pg_cron through the `cron` function every minute. It does
// the work nothing else triggers: renew subscriptions before they expire (FR-6), recreate
// missing ones, run the safety-net delta (FR-7) and any resync that couldn't run at the
// time (throttling, transient errors, a sync already in progress), and finish unlinks and
// account deletions that were interrupted.

import { SourceStatus } from "../db.ts";
import type { Services } from "../services.ts";
import { HOUR, MINUTE } from "../timeutil.ts";
import { deleteUser, purgeSource } from "./lifecycle.ts";
import { runTask } from "./runner.ts";
import { describeSync, requestResync, SyncMode, syncSource } from "./source_sync.ts";
import { createSubscription, renewSubscription } from "./subscriptions.ts";

const RENEW_EXPIRY_MARGIN_MS = 12 * HOUR;
const SETUP_GRACE_MS = 10 * MINUTE;
// An unlink or account deletion that hasn't finished by then was interrupted.
const STUCK_REMOVAL_MS = 10 * MINUTE;
const CONCURRENCY = 4;
// Stop starting new work after this long; whatever is left runs on the next sweep (a
// minute later). Syncs hold a lease, so an overlap with the next sweep is harmless.
const DEFAULT_BUDGET_MS = 50_000;

export interface SweepSummary {
  renew: number;
  recreate: number;
  delta: number;
  removals: number;
  deletions: number;
  skipped: number;
  results: Record<string, number>;
}

type Task = () => Promise<string>;

async function runPool(
  tasks: Task[],
  concurrency: number,
  deadline: number,
): Promise<{ results: string[]; skipped: number }> {
  const results: string[] = [];
  let next = 0;
  let skipped = 0;
  async function worker() {
    while (next < tasks.length) {
      const task = tasks[next++];
      if (Date.now() > deadline) {
        skipped++;
        continue;
      }
      results.push(await task());
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, tasks.length) }, worker));
  return { results, skipped };
}

export async function sweep(services: Services, { budgetMs = DEFAULT_BUDGET_MS } = {}): Promise<SweepSummary> {
  const { sql, settings } = services;
  const deadline = Date.now() + budgetMs;

  // FR-6: renew every subscription before it expires. Only for active sources: a source
  // waiting for its owner to reconnect is left alone.
  const renewals = await sql<{ id: string; source_id: string }[]>`
    select s.id, s.source_id from subscriptions s
    join linked_sources ls on ls.id = s.source_id
    where ls.status = ${SourceStatus.ACTIVE}
      and (s.renew_due_at <= now() or s.expires_at <= now() + make_interval(secs => ${RENEW_EXPIRY_MARGIN_MS / 1000}))
    order by s.expires_at
  `;
  const missing = await sql<{ id: string }[]>`
    select ls.id from linked_sources ls
    left join subscriptions s on s.source_id = ls.id
    where ls.status = ${SourceStatus.ACTIVE} and s.id is null
      and ls.created_at < now() - make_interval(secs => ${SETUP_GRACE_MS / 1000})
  `;
  // FR-7: periodic safety-net delta, plus resyncs asked for earlier. Skips sources whose
  // tenant is still throttled and sources another sync is working on.
  const due = await sql<{ id: string; mode: SyncMode }[]>`
    select ls.id,
      case when ls.resync_full then 'full'
           when ls.resync_requested_at is not null then 'recovery'
           else 'scheduled' end as mode
    from linked_sources ls join tenants t on t.id = ls.tenant_pk
    where ls.status = ${SourceStatus.ACTIVE}
      and (ls.last_synced_at is null
           or ls.last_synced_at < now() - make_interval(hours => ${settings.deltaResyncIntervalHours})
           or ls.resync_requested_at is not null)
      and (ls.sync_lease_until is null or ls.sync_lease_until < now())
      and (t.throttled_until is null or t.throttled_until < now())
    order by ls.resync_requested_at nulls last, ls.last_synced_at nulls first
  `;
  const removals = await sql<{ id: string }[]>`
    select id from linked_sources
    where status = ${SourceStatus.REMOVING}
      and updated_at < now() - make_interval(secs => ${STUCK_REMOVAL_MS / 1000})
  `;
  const deletions = await sql<{ id: string }[]>`
    select id from users
    where deletion_requested_at < now() - make_interval(secs => ${STUCK_REMOVAL_MS / 1000})
  `;

  const tasks: Task[] = [
    ...renewals.map((r) => () =>
      runTask(services, "renew_subscription", () => renewSubscription(services, r.id), { sourceId: r.source_id })
    ),
    ...missing.map((r) => () =>
      runTask(services, "ensure_subscription", async () => {
        const sub = await createSubscription(services, r.id);
        if (!sub) return "skipped";
        return `created ${describeSync(await syncSource(services, r.id, SyncMode.RECOVERY))}`;
      }, { sourceId: r.id })
    ),
    ...due.map((r) => () =>
      runTask(services, "sync_source", async () => describeSync(await syncSource(services, r.id, r.mode)), {
        sourceId: r.id,
        onTransient: () => requestResync(sql, r.id, r.mode === SyncMode.FULL),
      })
    ),
    ...removals.map((r) => () =>
      runTask(services, "remove_source", () => purgeSource(services, r.id, { reason: "unlink resumed by sweep" }))
    ),
    ...deletions.map((r) => () => runTask(services, "delete_user", () => deleteUser(services, r.id), { userId: r.id })),
  ];
  const { results, skipped } = await runPool(tasks, CONCURRENCY, deadline);
  const counts: Record<string, number> = {};
  for (const r of results) {
    const key = r.split(" ")[0];
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return {
    renew: renewals.length,
    recreate: missing.length,
    delta: due.length,
    removals: removals.length,
    deletions: deletions.length,
    skipped,
    results: counts,
  };
}
