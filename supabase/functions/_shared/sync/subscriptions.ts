// Graph change-notification subscriptions: create, renew, reauthorize, recreate.
//
// Subscriptions on a source mailbox are created with its owner's delegated token, so Graph
// ties them to that grant: when the grant goes away Graph stops delivering, and the source
// is marked reauth_required (see lifecycle.markReauthRequired). After the user reconnects,
// the subscription is recreated and a delta resync covers the gap.

import { SourceStatus, type SubscriptionRow, SubscriptionStatus } from "../db.ts";
import { parseIso } from "../events.ts";
import { GraphError, GraphNotFound } from "../graph/client.ts";
import { TokenError } from "../graph/tokens.ts";
import type { Services } from "../services.ts";
import { addMs, HOUR, isoformatZ, MINUTE, utcnow } from "../timeutil.ts";
import { SyncMode, syncSource } from "./source_sync.ts";

const RENEW_RETRY_MS = 15 * MINUTE;

function randomSecret(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Best effort: the subscription may already be gone, or the grant may be revoked. */
export async function deleteRemoteSubscription(
  services: Services,
  sourceId: string,
  tenantId: string,
  graphSubscriptionId: string,
): Promise<void> {
  try {
    await services.graph.source(sourceId, tenantId).deleteSubscription(graphSubscriptionId);
  } catch (err) {
    if (!(err instanceof GraphError || err instanceof TokenError)) throw err;
    console.info(`could not delete subscription ${graphSubscriptionId}: ${err.message}`);
  }
}

/** Create (or replace) the subscription for a source calendar. */
export async function createSubscription(services: Services, sourceId: string): Promise<SubscriptionRow | null> {
  const { sql, settings } = services;
  const [source] = await sql<
    {
      id: string;
      status: string;
      source_user_id: string;
      calendar_id: string | null;
      tenant_id: string | null;
      old_subscription_pk: string | null;
      old_graph_subscription_id: string | null;
    }[]
  >`
    select ls.id, ls.status, ls.source_user_id, ls.calendar_id, t.tenant_id,
           s.id as old_subscription_pk, s.graph_subscription_id as old_graph_subscription_id
    from linked_sources ls
    join tenants t on t.id = ls.tenant_pk
    left join subscriptions s on s.source_id = ls.id
    where ls.id = ${sourceId}
  `;
  if (!source || source.status !== SourceStatus.ACTIVE || !source.tenant_id) return null;
  const tenantId = source.tenant_id;
  const graph = services.graph.source(source.id, tenantId);

  if (source.old_subscription_pk) {
    await deleteRemoteSubscription(services, source.id, tenantId, source.old_graph_subscription_id!);
    await sql`delete from subscriptions where id = ${source.old_subscription_pk}`;
  }

  const now = utcnow();
  const lifetime = settings.subscriptionLifetimeMinutes * MINUTE;
  const clientState = randomSecret();
  const resource = graph.subscriptionResource(source.source_user_id, source.calendar_id);
  const created = await graph.createSubscription({
    changeType: "created,updated,deleted",
    notificationUrl: `${settings.publicApiUrl}/webhooks/graph`,
    lifecycleNotificationUrl: `${settings.publicApiUrl}/webhooks/lifecycle`,
    resource,
    expirationDateTime: isoformatZ(addMs(now, lifetime)),
    clientState,
  });
  const [sub] = await sql<SubscriptionRow[]>`
    insert into subscriptions (
      source_id, graph_subscription_id, client_state, resource, expires_at, renew_due_at, last_renewed_at
    ) values (
      ${source.id}, ${created.id}, ${await services.cipher.encrypt(clientState)}, ${resource},
      ${parseIso(created.expirationDateTime) ?? addMs(now, lifetime)},
      ${addMs(now, settings.subscriptionRenewIntervalHours * HOUR)}, ${now}
    )
    returning *
  `;
  console.info(`created subscription ${sub.graph_subscription_id} for source ${source.id}`);
  return sub;
}

export async function recreateAndResync(services: Services, sourceId: string, tenantId: string): Promise<void> {
  console.warn(`recreating subscription for source ${sourceId} (tenant ${tenantId})`);
  await createSubscription(services, sourceId);
  await syncSource(services, sourceId, SyncMode.RECOVERY);
}

export async function renewSubscription(services: Services, subscriptionPk: string): Promise<string> {
  const { sql, settings } = services;
  const [sub] = await sql<
    { id: string; graph_subscription_id: string; source_id: string; status: string; tenant_id: string }[]
  >`
    select s.id, s.graph_subscription_id, s.source_id, ls.status, t.tenant_id
    from subscriptions s
    join linked_sources ls on ls.id = s.source_id
    join tenants t on t.id = ls.tenant_pk
    where s.id = ${subscriptionPk}
  `;
  if (!sub) return "gone";
  if (sub.status !== SourceStatus.ACTIVE) {
    // Paused (reauth_required) or being removed: keep the row; reconnecting recreates the
    // subscription and unlinking deletes it.
    return `skipped-${sub.status}`;
  }

  const now = utcnow();
  const lifetime = settings.subscriptionLifetimeMinutes * MINUTE;
  let renewed;
  try {
    renewed = await services.graph
      .source(sub.source_id, sub.tenant_id)
      .renewSubscription(sub.graph_subscription_id, addMs(now, lifetime));
  } catch (err) {
    if (err instanceof GraphNotFound) {
      console.warn(`renewal failed tenant=${sub.tenant_id}: subscription ${sub.graph_subscription_id} vanished`);
      await recreateAndResync(services, sub.source_id, sub.tenant_id);
      return "recreated";
    }
    if (err instanceof GraphError && !err.transient) {
      console.warn(`renewal failed tenant=${sub.tenant_id}: ${err.message}`);
      await sql`
        update subscriptions
        set status = ${SubscriptionStatus.RENEW_FAILED}, last_error = ${err.message},
            renew_due_at = ${addMs(now, RENEW_RETRY_MS)}
        where id = ${sub.id}
      `;
    }
    throw err;
  }

  await sql`
    update subscriptions set
      expires_at = ${parseIso(renewed?.expirationDateTime) ?? addMs(now, lifetime)},
      renew_due_at = ${addMs(now, settings.subscriptionRenewIntervalHours * HOUR)},
      last_renewed_at = ${now},
      status = ${SubscriptionStatus.ACTIVE},
      last_error = null
    where id = ${sub.id}
  `;
  return "renewed";
}

export async function handleLifecycle(services: Services, graphSubscriptionId: string, event: string): Promise<string> {
  const [sub] = await services.sql<{ id: string; source_id: string; status: string; tenant_id: string }[]>`
    select s.id, s.source_id, ls.status, t.tenant_id
    from subscriptions s
    join linked_sources ls on ls.id = s.source_id
    join tenants t on t.id = ls.tenant_pk
    where s.graph_subscription_id = ${graphSubscriptionId}
  `;
  if (!sub) return "unknown-subscription";
  if (sub.status !== SourceStatus.ACTIVE) {
    // Nothing to do until the user reconnects (which recreates the subscription and
    // resyncs) or the unlink finishes.
    return `skipped-${sub.status}`;
  }
  if (event === "missed") {
    await syncSource(services, sub.source_id, SyncMode.RECOVERY);
    return "resync";
  }
  if (event === "subscriptionRemoved") {
    await recreateAndResync(services, sub.source_id, sub.tenant_id);
    return "recreated";
  }
  if (event === "reauthorizationRequired") {
    // Reauthorizing needs a currently valid delegated token: refreshing it here is what
    // proves the grant still exists. A TokenError(reauthRequired) propagates to the task
    // runner, which marks the source reauth_required.
    await services.tokens.invalidateSource(sub.source_id);
    try {
      await services.graph.source(sub.source_id, sub.tenant_id).reauthorizeSubscription(graphSubscriptionId);
      return "reauthorized";
    } catch (err) {
      if (err instanceof GraphNotFound) {
        await recreateAndResync(services, sub.source_id, sub.tenant_id);
        return "recreated";
      }
      if (!(err instanceof GraphError) || err.transient || err.authFailure) throw err;
      console.info(`reauthorize failed for ${graphSubscriptionId} (${err.message}); renewing instead`);
      return await renewSubscription(services, sub.id);
    }
  }
  console.warn(`ignoring unknown lifecycle event ${event}`);
  return "ignored";
}
