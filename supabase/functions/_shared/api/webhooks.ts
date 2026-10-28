// Graph change and lifecycle notification receivers (the `webhooks` function).
//
// Each delivery is verified (clientState, tenant) and acknowledged with 202 straight away;
// the work itself (fetch-on-notify, writing the event to the database, lifecycle
// handling) runs after the response, so a slow Graph read never delays the
// acknowledgement Graph is waiting for (< 3 s).

import { Hono } from "../deps.ts";
import { timingSafeEqual } from "../security.ts";
import type { Services } from "../services.ts";
import { runTask } from "../sync/runner.ts";
import { processNotification, requestResync } from "../sync/source_sync.ts";
import { handleLifecycle } from "../sync/subscriptions.ts";
import { type AppEnv, type Ctx, HttpError, installErrorHandler } from "./common.ts";

export interface Notification {
  subscriptionId: string;
  clientState?: string | null;
  tenantId?: string | null;
  changeType?: string | null;
  resource?: string | null;
  resourceData?: { id?: string } | null;
  lifecycleEvent?: string | null;
}

export function resourceId(n: Notification): string | null {
  if (n.resourceData?.id) return n.resourceData.id;
  if (n.resource) {
    let last = n.resource.replace(/\/+$/, "").split("/").pop()!;
    if (last.startsWith("Events('")) last = last.slice("Events('".length);
    if (last.endsWith("')")) last = last.slice(0, -2);
    return last;
  }
  return null;
}

/** Handshake: echo the token as text/plain within 10 seconds. */
function validationResponse(token: string): Response {
  return new Response(token, { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } });
}

async function parse(c: Ctx): Promise<Notification[]> {
  let items: unknown;
  try {
    items = (await c.req.json())?.value ?? [];
  } catch {
    throw new HttpError(400, "Invalid notification payload");
  }
  if (!Array.isArray(items)) throw new HttpError(400, "Invalid notification payload");
  const parsed: Notification[] = [];
  for (const item of items) {
    if (item && typeof item === "object" && typeof item.subscriptionId === "string") parsed.push(item);
    else console.warn("rejected notification: malformed");
  }
  return parsed;
}

/** Keep notifications whose clientState matches the secret stored for that subscription
 * and whose tenant matches the source. Returns [notification, tenant id, source id]. */
async function verified(services: Services, notifications: Notification[]) {
  const ids = [...new Set(notifications.map((n) => n.subscriptionId))];
  if (!ids.length) return [];
  const rows = await services.sql<
    { graph_subscription_id: string; client_state: string; tenant_id: string | null; source_id: string }[]
  >`
    select s.graph_subscription_id, s.client_state, t.tenant_id, s.source_id
    from subscriptions s
    join linked_sources ls on ls.id = s.source_id
    join tenants t on t.id = ls.tenant_pk
    where s.graph_subscription_id in ${services.sql(ids)}
  `;
  const known = new Map<string, { secret: string; tenantId: string | null; sourceId: string }>();
  for (const r of rows) {
    known.set(r.graph_subscription_id, {
      secret: await services.cipher.decrypt(r.client_state),
      tenantId: r.tenant_id,
      sourceId: r.source_id,
    });
  }
  const out: [Notification, string, string][] = [];
  for (const n of notifications) {
    const entry = known.get(n.subscriptionId);
    if (!entry) {
      console.warn(`rejected notification: unknown subscription ${n.subscriptionId}`);
      continue;
    }
    if (!n.clientState || !timingSafeEqual(entry.secret, n.clientState)) {
      console.warn(`rejected notification with bad clientState for ${n.subscriptionId}`);
      continue;
    }
    if (n.tenantId && entry.tenantId && n.tenantId.toLowerCase() !== entry.tenantId.toLowerCase()) {
      console.warn(`rejected notification: tenant mismatch for ${n.subscriptionId}`);
      continue;
    }
    out.push([n, entry.tenantId ?? "", entry.sourceId]);
  }
  return out;
}

export function createWebhooksApp(services: Services): Hono<AppEnv> {
  const app = new Hono<AppEnv>();
  installErrorHandler(app);

  app.post("/webhooks/graph", async (c) => {
    const token = c.req.query("validationToken");
    if (token !== undefined) return validationResponse(token);
    const started = performance.now();
    // Storms of notifications for one event collapse into one fetch per delivery.
    const seen = new Set<string>();
    for (const [n, tenantId, sourceId] of await verified(services, await parse(c))) {
      const id = resourceId(n);
      if (!id) {
        console.warn(`rejected notification: no resource for ${n.subscriptionId}`);
        continue;
      }
      if (seen.has(`${n.subscriptionId}:${id}`)) continue;
      seen.add(`${n.subscriptionId}:${id}`);
      console.info(`notification tenant=${tenantId} change=${n.changeType ?? "updated"}`);
      services.background(
        runTask(services, "process_notification", () => processNotification(services, n.subscriptionId, id), {
          sourceId,
          // A missed change is caught by the next delta.
          onTransient: () => requestResync(services.sql, sourceId),
        }),
      );
    }
    console.info(`webhook graph acknowledged in ${(performance.now() - started).toFixed(0)}ms`);
    return c.body(null, 202);
  });

  app.post("/webhooks/lifecycle", async (c) => {
    const token = c.req.query("validationToken");
    if (token !== undefined) return validationResponse(token);
    const seen = new Set<string>();
    for (const [n, tenantId, sourceId] of await verified(services, await parse(c))) {
      const event = n.lifecycleEvent;
      if (!event || seen.has(`${n.subscriptionId}:${event}`)) continue;
      seen.add(`${n.subscriptionId}:${event}`);
      console.info(`lifecycle ${event} for subscription ${n.subscriptionId} tenant=${tenantId}`);
      services.background(
        runTask(services, "handle_lifecycle", () => handleLifecycle(services, n.subscriptionId, event), {
          sourceId,
          // Renewing also reauthorizes, and a resync covers anything missed.
          onTransient: async () => {
            await services
              .sql`update subscriptions set renew_due_at = now() where graph_subscription_id = ${n.subscriptionId}`;
            await requestResync(services.sql, sourceId);
          },
        }),
      );
    }
    return c.body(null, 202);
  });

  return app;
}
