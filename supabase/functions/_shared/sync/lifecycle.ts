// Source and account lifecycle: setup, reauth_required, unlink, account deletion.

import { audit } from "../audit.ts";
import { SourceStatus, transaction } from "../db.ts";
import type { Services } from "../services.ts";
import { createSubscription, deleteRemoteSubscription } from "./subscriptions.ts";
import { SyncMode, syncSource } from "./source_sync.ts";

// --- sources -------------------------------------------------------------------------------

/** Start syncing a newly linked or reconnected source: subscribe, then sync.
 *
 * INITIAL backfills a new link; RECOVERY (after a reconnect) continues from the stored
 * delta link, which picks up everything that changed while the source was paused. */
export async function setupSource(services: Services, sourceId: string, mode: SyncMode = SyncMode.INITIAL) {
  const [source] = await services.sql<{ status: string }[]>`
    select status from linked_sources where id = ${sourceId}
  `;
  if (!source) return "gone";
  if (source.status !== SourceStatus.ACTIVE) return `skipped-${source.status}`;

  // Subscribe first, then sync: a change made in between is caught by the delta.
  // createSubscription replaces a subscription left over from a previous grant.
  await createSubscription(services, sourceId);
  const result = await syncSource(services, sourceId, mode);
  return `subscribed ${result.skipped ?? `pages=${result.pages} changed=${result.changedKeys.size}`}`;
}

/** The source's delegated grant stopped working (refresh token expired or revoked, MFA or
 * consent now required, password reset...). Pause syncing until the owner signs in again.
 * Stored events (still shown in the unified calendar) and the subscription row are kept
 * on purpose. */
export async function markReauthRequired(services: Services, sourceId: string, reason: string): Promise<string> {
  const outcome = await transaction(services.sql, async (tx) => {
    const [source] = await tx<
      { id: string; status: string; user_id: string; tenant_pk: string; source_email: string }[]
    >`
      select id, status, user_id, tenant_pk, source_email from linked_sources where id = ${sourceId} for update
    `;
    if (!source) return "gone";
    if (source.status === SourceStatus.REMOVING) return "removing";
    const first = source.status !== SourceStatus.REAUTH_REQUIRED;
    await tx`
      update linked_sources set
        status = ${SourceStatus.REAUTH_REQUIRED},
        last_error = ${reason.slice(0, 2000)},
        last_error_at = now(),
        reauth_required_at = case when ${first} then now() else reauth_required_at end,
        access_token = null,
        access_token_expires_at = null
      where id = ${sourceId}
    `;
    if (first) {
      await audit(tx, "source.reauth_required", "linked_source", sourceId, {
        email: source.source_email,
        reason: reason.slice(0, 500),
      });
      await tx`
        insert into sync_errors (user_id, tenant_pk, source_id, kind, message)
        values (${source.user_id}, ${source.tenant_pk}, ${sourceId}, 'source.reauth_required', ${reason.slice(0, 4000)})
      `;
      console.warn(`source ${sourceId} needs the user to reconnect: ${reason}`);
    }
    return "reauth-required";
  });
  return outcome;
}

/** Unlink: stop syncing a source, delete its stored events, which removes them from the
 * unified calendar (FR-12), and delete the link, including its refresh token. */
export async function purgeSource(services: Services, sourceId: string, { reason }: { reason: string }) {
  const { sql } = services;
  const [source] = await sql<{ id: string; tenant_id: string | null; graph_subscription_id: string | null }[]>`
    update linked_sources ls set status = ${SourceStatus.REMOVING}
    from tenants t
    where ls.id = ${sourceId} and t.id = ls.tenant_pk
    returning ls.id, t.tenant_id,
      (select graph_subscription_id from subscriptions s where s.source_id = ls.id) as graph_subscription_id
  `;
  if (!source) return "gone";

  if (source.graph_subscription_id && source.tenant_id) {
    // Best effort, with the source's own token (fails harmlessly if revoked).
    await deleteRemoteSubscription(services, source.id, source.tenant_id, source.graph_subscription_id);
  }
  const events = await transaction(sql, async (tx) => {
    const deleted = await tx`delete from source_events where source_id = ${sourceId} returning id`;
    // Subscription and token go with the row.
    await tx`delete from linked_sources where id = ${sourceId}`;
    await audit(tx, "source.purged", "linked_source", sourceId, { reason, events: deleted.length });
    return deleted.length;
  });
  return `purged events=${events}`;
}

// --- accounts ------------------------------------------------------------------------------

/** Account deletion: stop every subscription and delete all stored data for the user. */
export async function deleteUser(services: Services, userId: string): Promise<string> {
  const { sql } = services;
  const [user] = await sql<{ email: string }[]>`select email from users where id = ${userId}`;
  if (!user) return "gone";
  const sources = await sql<{ id: string; tenant_id: string | null; graph_subscription_id: string | null }[]>`
    update linked_sources ls set status = ${SourceStatus.REMOVING}
    from tenants t
    where ls.user_id = ${userId} and t.id = ls.tenant_pk
    returning ls.id, t.tenant_id,
      (select graph_subscription_id from subscriptions s where s.source_id = ls.id) as graph_subscription_id
  `;
  for (const source of sources) {
    if (source.graph_subscription_id && source.tenant_id) {
      await deleteRemoteSubscription(services, source.id, source.tenant_id, source.graph_subscription_id);
    }
  }
  await transaction(sql, async (tx) => {
    await audit(tx, "user.deleted", "user", userId, { email: user.email });
    // Linked sources, events and subscriptions cascade in the database.
    await tx`delete from users where id = ${userId}`;
  });
  return "deleted";
}
