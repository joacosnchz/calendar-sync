// End-to-end flows through the API, webhooks, background work, the sweep and mock Graph.

import { assert, assertEquals, assertNotEquals } from "jsr:@std/assert@1.0.13";
import { normalizeEvent } from "../_shared/events.ts";
import { syncSource, upsertEvents } from "../_shared/sync/source_sync.ts";
import { markReauthRequired } from "../_shared/sync/lifecycle.ts";
import { runTask } from "../_shared/sync/runner.ts";
import { describeSync } from "../_shared/sync/source_sync.ts";
import { type Browser, type Harness, Harness as H, type Json, MOCK_URL, testWithUsers } from "./harness.ts";

const ALICE = "alice@contoso.example";
const HOUR = 3_600_000;

const at = (value: string) => new Date(value).getTime();
const graphTime = (d: Date) => ({ dateTime: d.toISOString().slice(0, 19), timeZone: "UTC" });

function topOfHour(offsetMs: number): Date {
  const d = new Date(Date.now() + offsetMs);
  d.setUTCMinutes(0, 0, 0);
  return d;
}

async function sources(browser: Browser): Promise<Json[]> {
  return await browser.json("/api/sources");
}

testWithUsers("linking stores the refresh token and backfills, without admin consent", async (h, { alice }) => {
  await h.createSourceEvent(ALICE, "Existing meeting", {
    location: { displayName: "Room 7" },
    attendees: [{ emailAddress: { address: "bob@contoso.example" } }],
    body: { contentType: "text", content: "Agenda" },
  });

  // Alice consents for her own mailbox: no tenant onboarding, no admin involved.
  const start = await alice.get("/auth/link");
  const location = start.headers.get("location")!;
  assert(location.includes("/organizations/oauth2/v2.0/authorize"));
  assert(location.includes("Calendars.Read") && location.includes("offline_access"));
  const result = await h.link(alice, ALICE);
  assertEquals(result, { status: "linked", email: ALICE });
  await h.settle();

  const state = await h.mock.state();
  const owner = state.users[ALICE];
  assert(state.grants[ALICE].includes("Calendars.Read"));
  const source = await h.source(ALICE);
  assertEquals(source.status, "active");
  assert(source.refresh_token_plain && state.refresh_tokens.includes(source.refresh_token_plain));
  assertNotEquals(source.refresh_token, source.refresh_token_plain); // encrypted at rest
  assert(source.granted_scopes?.includes("Calendars.Read"));
  const [tenant] = await h.sql`select * from tenants`;
  assertEquals([tenant.tenant_id, tenant.domain], [owner.tenant_id, "contoso.example"]);

  const [sub] = state.subscriptions;
  assertEquals(state.subscriptions.length, 1);
  assertEquals(sub.resource, `users/${owner.oid}/events`);
  assertEquals(sub.creator_oid, owner.oid); // created with Alice's delegated token
  assertEquals(sub.change_types, ["created", "deleted", "updated"]);

  const events = await h.calendar(alice);
  assertEquals(events.length, 1);
  const [event] = events;
  assertEquals([event.subject, event.location], ["Existing meeting", "Room 7"]);
  assertEquals(event.labels, ["Contoso"]);
  assertEquals(event.source_ids, [source.id]);
  assertEquals(
    Object.keys(event).sort(),
    ["end", "is_all_day", "key", "labels", "location", "show_as", "source_ids", "start", "subject"],
  );
  // FR-9: attendees and body are never stored.
  const columns = await h.sql`
    select column_name from information_schema.columns where table_name = 'source_events'
      and column_name in ('attendees', 'body')
  `;
  assertEquals(columns.length, 0);

  const [out] = await sources(alice);
  assertEquals(out.status, "active");
  assertEquals([out.reauth_url, out.reauth_required_at], [null, null]);
  assert(!("consent_url" in out) && !("consent_status" in out.tenant));
  assertEquals(out.subscription.status, "active");
  assertEquals(out.event_count, 1);
  assert(out.last_full_sync_at !== null);
});

testWithUsers("notifications create, update, cancel and delete events", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);

  const created = await h.createSourceEvent(ALICE, "Kickoff", { showAs: "tentative" });
  await h.settle();
  let [event] = await h.calendar(alice);
  assertEquals([event.subject, event.show_as], ["Kickoff", "tentative"]);

  const moved = topOfHour(48 * HOUR);
  await h.mock.updateEvent(ALICE, created.id, {
    subject: "Kickoff (moved)",
    start: graphTime(moved),
    end: graphTime(new Date(moved.getTime() + HOUR)),
  });
  await h.settle();
  [event] = await h.calendar(alice);
  assertEquals(event.subject, "Kickoff (moved)");
  assertEquals([at(event.start), at(event.end)], [moved.getTime(), moved.getTime() + HOUR]);

  await h.mock.updateEvent(ALICE, created.id, { isCancelled: true });
  await h.settle();
  assertEquals(await h.calendar(alice), []);

  const second = await h.createSourceEvent(ALICE, "Standup");
  await h.settle();
  assertEquals(await h.subjects(alice), ["Standup"]);
  await h.mock.deleteEvent(ALICE, second.id);
  await h.settle();
  assertEquals(await h.calendar(alice), []);
  assertEquals(await h.sourceEventCount(), 0);
});

async function notify(h: Harness, sub: Json, eventIds: string[]): Promise<void> {
  const resp = await h.webhooks.request("/webhooks/graph", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      value: eventIds.map((id) => ({
        subscriptionId: sub.id,
        changeType: "updated",
        resource: `Users/${sub.owner_oid}/Events/${id}`,
        resourceData: { id },
        clientState: sub.client_state,
        tenantId: sub.tenant_id,
      })),
    }),
  });
  assertEquals(resp.status, 202);
}

testWithUsers("duplicate and stale notifications are harmless", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const event = await h.createSourceEvent(ALICE, "Once");
  await h.settle();
  const [sub] = await h.mock.subscriptions();
  const [before] = await h.sql`select updated_at from source_events`;

  // Graph may deliver the same notification several times in one batch: one fetch.
  const started = h.started;
  await notify(h, sub, Array(5).fill(event.id));
  assertEquals(h.started, started + 1);
  // Re-delivery after processing re-fetches but writes nothing (lastModified unchanged).
  await notify(h, sub, [event.id]);
  await h.settle();
  assertEquals(await h.subjects(alice), ["Once"]);
  assertEquals(await h.sourceEventCount(), 1);
  const [after] = await h.sql`select updated_at from source_events`;
  assertEquals(after.updated_at, before.updated_at);
});

testWithUsers("the same meeting in two sources shows once", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  await h.linkAndSync(alice, "alice@fabrikam.example");

  const original = await h.createSourceEvent(ALICE, "Partnership", { showAs: "tentative" });
  await h.createSourceEvent("alice@fabrikam.example", "Partnership", { icalUid: original.iCalUId });
  await h.settle();
  assertEquals(await h.sourceEventCount(), 2);
  const byEmail: Record<string, Json> = Object.fromEntries((await sources(alice)).map((s) => [s.source_email, s]));
  // FR-14: one entry for both copies; FR-10: the busiest show-as wins.
  let [event] = await h.calendar(alice);
  assertEquals([event.subject, event.key], ["Partnership", `ical:${original.iCalUId}`]);
  assertEquals([event.labels, event.show_as], [["Contoso", "Fabrikam"], "busy"]);
  assertEquals([...event.source_ids].sort(), Object.values(byEmail).map((s) => s.id).sort());

  // Unlinking one source keeps the meeting (still in the other) and relabels it.
  const resp = await alice.delete(`/api/sources/${byEmail["alice@fabrikam.example"].id}`);
  assertEquals(resp.status, 202);
  await h.settle();
  [event] = await h.calendar(alice);
  assertEquals([event.labels, event.show_as], [["Contoso"], "tentative"]);
  assertEquals(event.source_ids, [byEmail[ALICE].id]);
  assertEquals((await h.mock.subscriptions()).length, 1);
  assertEquals((await sources(alice)).map((s) => s.source_email), [ALICE]);
});

testWithUsers("unlinking removes the source's events and data", async (h, { alice }) => {
  await h.createSourceEvent(ALICE, "A");
  await h.createSourceEvent(ALICE, "B", { hoursFromNow: 48 });
  await h.linkAndSync(alice, ALICE);
  assertEquals(await h.subjects(alice), ["A", "B"]);

  const [source] = await sources(alice);
  assertEquals((await alice.delete(`/api/sources/${source.id}`)).status, 202);
  // Hidden from the calendar as soon as the unlink is requested (status "removing").
  assertEquals(await h.calendar(alice), []);
  await h.settle();
  assertEquals(await h.mock.subscriptions(), []); // deleted remotely with Alice's token
  assertEquals(await h.sourceEventCount(), 0);
  assertEquals((await h.sql`select id from linked_sources`).length, 0);
  const [purged] = await h.sql`select details from audit_log where action = 'source.purged'`;
  assertEquals(purged.details.events, 2);
});

/** Alice's Contoso source, synced with one event, then her refresh tokens revoked. */
async function pausedByRevocation(h: Harness, alice: Browser) {
  await h.createSourceEvent(ALICE, "Board prep");
  await h.linkAndSync(alice, ALICE);
  assertEquals(await h.subjects(alice), ["Board prep"]);
  // Revoked sessions (password reset, admin revocation...): Graph asks us to reauthorize
  // the delegated subscription, and refreshing the token fails.
  await h.mock.revoke(ALICE);
  await h.settle();
  const source = await h.source(ALICE);
  assertEquals(source.status, "reauth_required");
  return source;
}

testWithUsers("a revoked refresh token pauses the source and keeps its events", async (h, { alice }) => {
  const source = await pausedByRevocation(h, alice);
  assert(source.reauth_required_at !== null);
  assert(source.last_error?.includes("AADSTS700082"));
  // Nothing is deleted: stored events (still in the calendar) and the subscription row stay.
  assertEquals(await h.subjects(alice), ["Board prep"]);
  assertEquals(await h.sourceEventCount(), 1);
  assertEquals((await h.subscriptionIds()).length, 1);
  assertEquals(source.access_token, null);

  const [out] = await sources(alice);
  assertEquals(out.status, "reauth_required");
  assertEquals(out.reauth_url, `/auth/reauth/${source.id}`);
  assert(out.reauth_required_at !== null && out.last_error.includes("AADSTS700082"));

  assertEquals((await h.sql`select 1 from audit_log where action = 'source.reauth_required'`).length, 1);
  assertEquals((await h.sql`select kind from sync_errors`).map((r) => r.kind), ["source.reauth_required"]);

  // Paused sources are skipped by every sweep and their events stay visible.
  await h
    .sql`update linked_sources set last_synced_at = now() - interval '7 hours', created_at = now() - interval '1 hour'`;
  await h.sql`update subscriptions set renew_due_at = now() - interval '1 minute'`;
  const summary = await h.sweep();
  assertEquals([summary.delta, summary.renew, summary.recreate], [0, 0, 0]);
  assertEquals(await h.subjects(alice), ["Board prep"]);
  // Resync is refused and a direct sync no-ops without calling Graph.
  assertEquals((await alice.post(`/api/sources/${source.id}/resync`)).status, 409);
  assertEquals((await syncSource(h.services, source.id, "full")).skipped, "inactive");

  // Changes made while paused aren't delivered (the grant is gone)...
  const started = h.started;
  await h.createSourceEvent(ALICE, "While paused");
  await h.settle();
  assertEquals(h.started, started);
  assertEquals((await h.mock.state()).deliveries.at(-1).kind, "suspended");
  // ...and a second failure doesn't add another audit entry or sync error.
  assertEquals(await markReauthRequired(h.services, source.id, "again"), "reauth-required");
  assertEquals((await h.sql`select id from sync_errors`).length, 1);
});

testWithUsers("reconnecting reactivates, resubscribes and resyncs", async (h, { alice }) => {
  const source = await pausedByRevocation(h, alice);
  const [oldSub] = await h.subscriptionIds();
  await h.createSourceEvent(ALICE, "While paused");
  await h.settle();
  assertEquals(await h.subjects(alice), ["Board prep"]);

  // Reconnect goes straight to Contoso's own login page with Alice's account pre-filled.
  const start = await alice.get(`/auth/reauth/${source.id}`);
  const location = start.headers.get("location")!;
  assert(location.startsWith(`${MOCK_URL}/${await h.mock.tenantOf(ALICE)}/oauth2/`), location);
  assert(location.includes("login_hint=alice%40contoso.example") && location.includes("Calendars.Read"));

  assertEquals(await h.reauth(alice, source.id, ALICE), { status: "reconnected", email: ALICE });
  const reconnected = await h.source(ALICE);
  assertEquals(reconnected.status, "active");
  assertEquals([reconnected.reauth_required_at, reconnected.last_error], [null, null]);
  assert(reconnected.refresh_token_plain && reconnected.refresh_token_plain !== source.refresh_token_plain);

  await h.settle();
  const [newSub] = await h.subscriptionIds();
  const remote = (await h.mock.subscriptions()).map((s: Json) => s.id);
  assertNotEquals(newSub, oldSub);
  assert(!remote.includes(oldSub) && remote.includes(newSub));
  assertEquals(await h.subjects(alice), ["Board prep", "While paused"]);

  // Notifications flow again on the new subscription.
  await h.createSourceEvent(ALICE, "After reconnect", { hoursFromNow: 30 });
  await h.settle();
  assert((await h.subjects(alice)).includes("After reconnect"));
  const [out] = await sources(alice);
  assertEquals([out.status, out.reauth_url], ["active", null]);
});

testWithUsers("reconnecting with the wrong account is rejected", async (h, { alice }) => {
  const source = await pausedByRevocation(h, alice);
  const started = h.started;
  let result = await h.reauth(alice, source.id, "bob@contoso.example");
  assertEquals(result, {
    status: "error",
    message: "You signed in as bob@contoso.example. Sign in as alice@contoso.example to reconnect this calendar.",
  });
  // An account from another tenant can't even complete sign-in on Contoso's login page.
  result = await h.reauth(alice, source.id, "alice@fabrikam.example");
  assertEquals(result.status, "error");
  assertEquals((await h.source(ALICE)).status, "reauth_required");
  assertEquals(h.started, started);

  // Another platform user can't reconnect (or probe) Alice's source.
  const bob = await h.signIn("bob@contoso.example");
  assertEquals(H.resultOf(await bob.get(`/auth/reauth/${source.id}`)), {
    status: "error",
    message: "Calendar not found",
  });
});

testWithUsers("linking again reconnects a paused source", async (h, { alice }) => {
  await pausedByRevocation(h, alice);
  assertEquals(await h.link(alice, ALICE), { status: "reconnected", email: ALICE });
  await h.settle();
  assertEquals((await h.source(ALICE)).status, "active");
  assertEquals((await h.mock.subscriptions()).length, 1);
  // Re-linking an already active source is just "linked".
  assertEquals((await h.link(alice, ALICE)).status, "linked");
});

testWithUsers("withdrawn consent pauses the source", async (h, { alice }) => {
  await h.createSourceEvent(ALICE, "Keep");
  await h.linkAndSync(alice, ALICE);
  await h.mock.revoke(ALICE, { withdraw_consent: true });
  await h.settle();
  const source = await h.source(ALICE);
  assertEquals(source.status, "reauth_required");
  assert(source.last_error?.includes("AADSTS65001"));
  assertEquals(await h.subjects(alice), ["Keep"]);
});

testWithUsers("Graph 403 on the user's own mailbox pauses the source", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const source = await h.source(ALICE);
  const owner = await h.mock.user(ALICE);
  // A token that no longer carries Calendars.Read (e.g. the grant was reduced).
  const weak = await h.mock.sign({
    kind: "delegated",
    tid: owner.tenant_id,
    oid: owner.oid,
    appid: h.settings.sourceAppClientId,
    scp: "User.Read",
    gen: owner.session_generation,
  });
  await h.services.tokens.cacheSourceToken(source.id, weak, 3600);
  const outcome = await runTask(
    h.services,
    "sync_source",
    async () => describeSync(await syncSource(h.services, source.id, "full")),
    { sourceId: source.id },
  );
  assertEquals(outcome, "reauth-required");
  const paused = await h.source(ALICE);
  assertEquals(paused.status, "reauth_required");
  assert(paused.last_error?.includes("403"));
});

testWithUsers("a paused source can still be unlinked", async (h, { alice }) => {
  const source = await pausedByRevocation(h, alice);
  assertEquals((await alice.delete(`/api/sources/${source.id}`)).status, 202);
  await h.settle();
  assertEquals(await h.calendar(alice), []);
  assertEquals(await h.sourceEventCount(), 0);
  assertEquals(await sources(alice), []);
});

testWithUsers("missed notifications are recovered by delta", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const tenantId = await h.mock.tenantOf(ALICE);
  await h.mock.tenant(tenantId, { drop_notifications: true });
  await h.createSourceEvent(ALICE, "Lost in transit");
  await h.settle();
  assertEquals(await h.calendar(alice), []);

  await h.mock.lifecycle(tenantId, "missed");
  await h.settle();
  assertEquals(await h.subjects(alice), ["Lost in transit"]);
});

testWithUsers("a removed subscription is recreated", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const [old] = await h.mock.subscriptions();
  await h.mock.lifecycle(await h.mock.tenantOf(ALICE), "subscriptionRemoved");
  await h.settle();
  const subs = await h.mock.subscriptions();
  assertEquals(subs.length, 1);
  assertNotEquals(subs[0].id, old.id);

  await h.createSourceEvent(ALICE, "After recreate");
  await h.settle();
  assertEquals(await h.subjects(alice), ["After recreate"]);
});

testWithUsers("reauthorizationRequired is handled with a fresh delegated token", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const before = await h.source(ALICE);
  await h.mock.lifecycle(await h.mock.tenantOf(ALICE), "reauthorizationRequired");
  await h.settle();
  // Reauthorized with a freshly refreshed delegated token (rotation persisted).
  const after = await h.source(ALICE);
  assertEquals(after.status, "active");
  assertNotEquals(after.refresh_token_plain, before.refresh_token_plain);
});

testWithUsers("renewal extends subscriptions and recreates lost ones", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  // Nothing due right after creation.
  assertEquals((await h.sweep()).renew, 0);

  await h.sql`update subscriptions set renew_due_at = now() - interval '1 minute'`;
  assertEquals((await h.sweep()).renew, 1);
  const [sub] = await h.sql`select * from subscriptions`;
  assert(sub.renew_due_at.getTime() > Date.now() + 23 * HOUR);
  assert(sub.expires_at.getTime() > Date.now() + 69 * HOUR);
  assertEquals(sub.status, "active");

  // Subscription silently gone at Graph: renewal recreates it and resyncs.
  await h.mock.forgetSubscriptions();
  await h.sql`update subscriptions set renew_due_at = now() - interval '1 minute'`;
  await h.sweep();
  const remote = await h.mock.subscriptions();
  assertEquals(remote.length, 1);
  assertEquals(await h.subscriptionIds(), [remote[0].id]);
});

testWithUsers("sources without a subscription get one", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  await h.sql`delete from subscriptions`;
  await h.sql`update linked_sources set created_at = now() - interval '1 hour'`;
  assertEquals((await h.sweep()).recreate, 1);
  assertEquals((await h.subscriptionIds()).length, 1);
});

testWithUsers("the scheduled delta catches silent changes", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  await h.mock.tenant(await h.mock.tenantOf(ALICE), { drop_notifications: true });
  const event = await h.createSourceEvent(ALICE, "Silent");
  await h.settle();
  assertEquals((await h.sweep()).delta, 0); // synced just now

  await h.sql`update linked_sources set last_synced_at = now() - interval '7 hours'`;
  assertEquals((await h.sweep()).delta, 1);
  assertEquals(await h.subjects(alice), ["Silent"]);

  // A deletion that was never notified disappears on the next delta too.
  await h.mock.deleteEvent(ALICE, event.id);
  await h.sql`update linked_sources set last_synced_at = now() - interval '7 hours'`;
  await h.sweep();
  assertEquals(await h.calendar(alice), []);
});

testWithUsers("full re-baseline and pagination", async (h, { alice }) => {
  await h.mock.config({ max_page_size: 2 });
  for (let i = 0; i < 5; i++) await h.createSourceEvent(ALICE, `Event ${i}`, { hoursFromNow: 24 + i });
  await h.linkAndSync(alice, ALICE);
  assertEquals((await h.calendar(alice)).length, 5);

  const [source] = await sources(alice);
  assertEquals((await alice.post(`/api/sources/${source.id}/resync`)).status, 202);
  await h.settle();
  const result = await syncSource(h.services, source.id, "full");
  assertEquals([result.pages, result.changedKeys.size, result.rebaseline], [3, 0, true]);
  assertEquals((await h.calendar(alice)).length, 5);
});

testWithUsers("large batches are written in chunks, and identical rewrites change nothing", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const source = await h.source(ALICE);
  const start = topOfHour(24 * HOUR);
  const events = Array.from({ length: 450 }, (_, i) =>
    normalizeEvent({
      id: `AAMkBulk${i}`,
      iCalUId: `BULK-${i}`,
      subject: `Bulk ${i}`,
      start: graphTime(new Date(start.getTime() + i * HOUR)),
      end: graphTime(new Date(start.getTime() + (i + 1) * HOUR)),
      lastModifiedDateTime: "2026-10-20T08:00:00Z",
    }));
  assertEquals((await upsertEvents(h.services, source.id, events, { authoritative: true })).size, 450);
  assertEquals(await h.sourceEventCount(), 450);
  assertEquals((await upsertEvents(h.services, source.id, events, { authoritative: true })).size, 0);
  assertEquals((await upsertEvents(h.services, source.id, events, { authoritative: false })).size, 0);
});

testWithUsers("an expired delta token re-baselines", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  // A delta token Graph no longer recognises (410 Gone / resync required).
  const owner = await h.mock.user(ALICE);
  const stale = btoa(
    JSON.stringify({ cal: "expired", seq: 0, start: "2026-01-01T00:00:00Z", end: "2027-01-01T00:00:00Z" }),
  )
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  await h.sql`
    update linked_sources
    set delta_link = ${`${MOCK_URL}/v1.0/users/${owner.oid}/calendarView/delta?$deltatoken=${stale}`}
  `;
  await h.mock.tenant(owner.tenant_id, { drop_notifications: true });
  await h.createSourceEvent(ALICE, "After expiry");
  await h.settle();
  const [source] = await sources(alice);
  const result = await syncSource(h.services, source.id, "recovery");
  assert(result.rebaseline);
  assertEquals(await h.subjects(alice), ["After expiry"]);
});

testWithUsers("a short throttle is waited out in place", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const tenantId = await h.mock.tenantOf(ALICE);
  await h.mock.tenant(tenantId, { throttle_remaining: 1 });
  await h.createSourceEvent(ALICE, "Busy tenant");
  await h.settle();
  assertEquals(await h.subjects(alice), ["Busy tenant"]);
  const [tenant] = await h.sql`select throttled_until from tenants where tenant_id = ${tenantId}`;
  assert(tenant.throttled_until !== null); // the back-off Graph asked for was recorded
});

testWithUsers("a long throttle is left to the sweep", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const tenantId = await h.mock.tenantOf(ALICE);
  // Graph asked this tenant to back off for longer than is worth waiting in place.
  await h.sql`update tenants set throttled_until = now() + interval '5 minutes' where tenant_id = ${tenantId}`;
  await h.createSourceEvent(ALICE, "Throttled");
  await h.settle();
  assertEquals(await h.calendar(alice), []);
  const source = await h.source(ALICE);
  assert(source.resync_requested_at !== null);
  // Not an error: nothing is recorded for the operator.
  assertEquals((await h.sql`select id from sync_errors`).length, 0);

  // While the tenant is still backing off, the sweep leaves it alone...
  assertEquals((await h.sweep()).delta, 0);
  // ...and once it has passed, the requested resync runs.
  await h.sql`update tenants set throttled_until = null`;
  assertEquals((await h.sweep()).delta, 1);
  assertEquals(await h.subjects(alice), ["Throttled"]);
  assertEquals((await h.source(ALICE)).resync_requested_at, null);
});

testWithUsers("repeated throttling is waited out within the budget", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  // Five 429s in a row (Retry-After: 2 each): still well inside the in-place budget.
  await h.mock.tenant(await h.mock.tenantOf(ALICE), { throttle_remaining: 5 });
  await h.createSourceEvent(ALICE, "Very busy tenant");
  await h.settle();
  assertEquals(await h.subjects(alice), ["Very busy tenant"]);
  assertEquals((await h.source(ALICE)).resync_requested_at, null);
});

testWithUsers("only one sync runs per source; a second is left to the sweep", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const source = await h.source(ALICE);
  const results = await Promise.all([1, 2, 3].map(() => syncSource(h.services, source.id, "scheduled")));
  assertEquals(results.filter((r) => r.skipped === "busy").length, 2);
  const leased = await h.source(ALICE);
  assertEquals(leased.sync_lease_until, null); // released
  assert(leased.resync_requested_at !== null);
  assertEquals((await h.sweep()).delta, 1);
  assertEquals((await h.source(ALICE)).resync_requested_at, null);
});

testWithUsers("a sync cut short by the platform is run again by the sweep", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  assertEquals((await h.source(ALICE)).resync_requested_at, null); // finished runs leave nothing behind
  await h.mock.tenant(await h.mock.tenantOf(ALICE), { drop_notifications: true });
  await h.createSourceEvent(ALICE, "Missed while the sync was killed");
  // As if an instance took the lease for a recovery sync and was then killed: the lease
  // expires and the request it recorded is still there.
  await h.sql`
    update linked_sources
    set sync_lease_until = now() + interval '10 minutes', resync_requested_at = now()
  `;
  assertEquals((await h.sweep()).delta, 0); // still leased
  await h.sql`update linked_sources set sync_lease_until = now() - interval '1 second'`;
  assertEquals((await h.sweep()).delta, 1);
  assertEquals(await h.subjects(alice), ["Missed while the sync was killed"]);
  assertEquals((await h.source(ALICE)).resync_requested_at, null);
});

testWithUsers("time zones are converted for the calendar", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const day = new Date(Date.now() + 3 * 86_400_000).toISOString().slice(0, 10);
  await h.mock.createEvent(ALICE, {
    subject: "West coast call",
    start: { dateTime: `${day}T09:00:00`, timeZone: "Pacific Standard Time" },
    end: { dateTime: `${day}T10:00:00`, timeZone: "Pacific Standard Time" },
  });
  await h.settle();
  const [event] = await h.calendar(alice);
  // 09:00 in Los Angeles, whichever of PST/PDT applies that day.
  const local = new Intl.DateTimeFormat("en-GB", {
    timeZone: "America/Los_Angeles",
    hour: "2-digit",
    minute: "2-digit",
  });
  assertEquals(local.format(new Date(event.start)), "09:00");
  assertEquals(at(event.end) - at(event.start), HOUR);
  const [stored] = await h.sql`select start_tz from source_events`;
  assertEquals(stored.start_tz, "Pacific Standard Time");
});

testWithUsers("an all-day event keeps its date", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const day = new Date(Date.now() + 4 * 86_400_000);
  const date = day.toISOString().slice(0, 10);
  const next = new Date(day.getTime() + 86_400_000).toISOString().slice(0, 10);
  await h.mock.createEvent(ALICE, {
    subject: "Offsite",
    isAllDay: true,
    start: { dateTime: `${date}T00:00:00`, timeZone: "Pacific Standard Time" },
    end: { dateTime: `${next}T00:00:00`, timeZone: "Pacific Standard Time" },
  });
  await h.settle();
  const [event] = await h.calendar(alice);
  assert(event.is_all_day);
  assertEquals(event.start.slice(0, 10), date);
});

testWithUsers("privacy mode shows busy only", async (h, { alice, operator }) => {
  const secret = await h.createSourceEvent(ALICE, "Secret merger", { location: { displayName: "Vault" } });
  await h.linkAndSync(alice, ALICE);
  assertEquals(await h.subjects(alice), ["Secret merger"]);

  // "Hide details" applies as soon as it's set: the calendar reads it every time.
  const [tenant] = await operator.json("/api/admin/tenants");
  const started = h.started;
  const resp = await operator.patch(`/api/admin/tenants/${tenant.id}`, { copy_details: false });
  assertEquals(resp.status, 200);
  assertEquals((await resp.json()).copy_details, false);
  assertEquals(h.started, started);
  let [event] = await h.calendar(alice);
  assertEquals([event.subject, event.location, event.labels], ["Busy", "", ["Contoso"]]);

  // The same meeting from a tenant that allows details still shows "Busy".
  await h.linkAndSync(alice, "alice@fabrikam.example");
  await h.createSourceEvent("alice@fabrikam.example", "Secret merger", {
    location: { displayName: "Vault" },
    icalUid: secret.iCalUId,
  });
  await h.settle();
  [event] = await h.calendar(alice);
  assertEquals([event.subject, event.labels], ["Busy", ["Contoso", "Fabrikam"]]);

  await operator.patch(`/api/admin/tenants/${tenant.id}`, { copy_details: true });
  [event] = await h.calendar(alice);
  assertEquals([event.subject, event.location], ["Secret merger", "Vault"]);
});

testWithUsers("a renamed label shows in the calendar", async (h, { alice }) => {
  await h.createSourceEvent(ALICE, "Sync");
  await h.linkAndSync(alice, ALICE);
  const [source] = await sources(alice);
  const started = h.started;
  const resp = await alice.patch(`/api/sources/${source.id}`, { label: "Client A" });
  assertEquals(resp.status, 200);
  const body = await resp.json();
  assertEquals([body.label, body.event_count], ["Client A", 1]);
  assertEquals(h.started, started); // nothing to re-render
  const [event] = await h.calendar(alice);
  assertEquals([event.labels, event.subject], [["Client A"], "Sync"]);
  assertEquals((await alice.patch(`/api/sources/${source.id}`, { label: "" })).status, 422);
});

testWithUsers("users are isolated", async (h, { alice }) => {
  await h.createSourceEvent(ALICE, "Alice only");
  await h.linkAndSync(alice, ALICE);

  const bob = await h.signIn("bob@contoso.example");
  const [aliceSource] = await sources(alice);
  assertEquals(await sources(bob), []);
  assertEquals((await bob.delete(`/api/sources/${aliceSource.id}`)).status, 404);
  assertEquals((await bob.patch(`/api/sources/${aliceSource.id}`, { label: "x" })).status, 404);
  assertEquals(await h.calendar(bob), []);

  // Bob can't link Alice's mailbox: linking requires signing in *as* that mailbox, and
  // linking bob's own mailbox gives bob his own, separate calendar.
  await h.createSourceEvent("bob@contoso.example", "Bob only");
  await h.linkAndSync(bob, "bob@contoso.example");
  assertEquals(await h.subjects(alice), ["Alice only"]);
  assertEquals(await h.subjects(bob), ["Bob only"]);
});

testWithUsers("the calendar range is limited", async (_h, { alice }) => {
  const now = Date.now();
  const query = (start: number, end: number) =>
    `/api/calendar?${new URLSearchParams({ start: new Date(start).toISOString(), end: new Date(end).toISOString() })}`;
  assertEquals((await alice.get(query(now, now - HOUR))).status, 400);
  assertEquals((await alice.get(query(now, now + 63 * 86_400_000))).status, 400);
  const ok = await alice.get(query(now, now + 62 * 86_400_000));
  assertEquals([ok.status, await ok.json()], [200, []]);
  assertEquals((await alice.get("/api/calendar?start=yesterday&end=today")).status, 422);
});

testWithUsers("source calendars are never written (FR-11)", async (h, { alice }) => {
  await h.createSourceEvent(ALICE, "Read me");
  await h.linkAndSync(alice, ALICE);
  const before = await h.mock.events(ALICE);
  await h.createSourceEvent(ALICE, "Another");
  await h.settle();
  const [source] = await sources(alice);
  await alice.delete(`/api/sources/${source.id}`);
  await h.settle();
  const after = await h.mock.events(ALICE);
  for (const ev of before) assertEquals(after.find((e: Json) => e.id === ev.id), ev);
  assertEquals(after.length, before.length + 1);
});

testWithUsers("blocked user consent means an admin must approve", async (h, { alice }) => {
  await h.mock.tenant(await h.mock.tenantOf(ALICE), { user_consent_blocked: true });
  const started = h.started;
  const result = await h.link(alice, ALICE);
  assertEquals(result.status, "admin_approval_required");
  assert(result.message.includes("AADSTS90094"));
  assertEquals(await sources(alice), []);
  assertEquals(h.started, started);
});

testWithUsers("declined consent is a plain error", async (h) => {
  const resp = await h.browser().get(
    `/auth/callback?${new URLSearchParams({
      error: "access_denied",
      error_description: "AADSTS65004: User declined to consent.",
    })}`,
  );
  assertEquals(H.resultOf(resp), { status: "error", message: "AADSTS65004: User declined to consent." });
});

testWithUsers("deleting an account removes everything", async (h, { alice }) => {
  await h.createSourceEvent(ALICE, "Bye");
  await h.linkAndSync(alice, ALICE);
  await h.linkAndSync(alice, "alice@fabrikam.example");

  const resp = await alice.delete("/api/me");
  assertEquals(resp.status, 202);
  assertEquals(alice.cookie("cs_session"), undefined);
  await h.settle();
  assertEquals(await h.mock.subscriptions(), []);
  assertEquals(await h.sourceEventCount(), 0);
  assertEquals((await h.sql`select id from linked_sources`).length, 0);
  assertEquals((await h.sql`select email from users`).map((u) => u.email), ["ops@calsync.example"]);
  const [deleted] = await h.sql`select details from audit_log where action = 'user.deleted'`;
  assertEquals(deleted.details.email, ALICE);
});

testWithUsers("interrupted unlinks and account deletions are finished by the sweep", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  await h.linkAndSync(alice, "alice@fabrikam.example");
  const contoso = await h.source(ALICE);
  // As if the function instance died right after the unlink was requested.
  await h.sql`
    update linked_sources set status = 'removing', updated_at = now() - interval '1 hour' where id = ${contoso.id}
  `;
  let summary = await h.sweep();
  assertEquals(summary.removals, 1);
  assertEquals((await sources(alice)).map((s) => s.source_email), ["alice@fabrikam.example"]);

  const [user] = await h.sql`select id from users where email = ${ALICE}`;
  await h.sql`update users set deletion_requested_at = now() - interval '1 hour' where id = ${user.id}`;
  summary = await h.sweep();
  assertEquals(summary.deletions, 1);
  assertEquals((await h.sql`select id from users where email = ${ALICE}`).length, 0);
  assertEquals(await h.mock.subscriptions(), []);
});

testWithUsers("operator health views", async (h, { alice, operator }) => {
  await h.linkAndSync(alice, ALICE);
  await h.createSourceEvent(ALICE, "One");
  await h.createSourceEvent(ALICE, "Two", { hoursFromNow: 30 });
  await h.settle();
  let overview = await operator.json("/api/admin/overview");
  assertEquals([overview.users, overview.tenants, overview.subscriptions, overview.events], [2, 1, 1, 2]);
  assertEquals(overview.sources, { active: 1 });
  assert(!("mirrors" in overview));
  let [tenant] = await operator.json("/api/admin/tenants");
  assertEquals([tenant.active_source_count, tenant.source_count, tenant.reauth_required_count], [1, 1, 0]);
  assert(tenant.next_subscription_expiry);
  assertEquals(tenant.tenant_id, await h.mock.tenantOf(ALICE));
  for (const gone of ["consent_status", "connected_at", "revoked_at", "consent_url"]) assert(!(gone in tenant));
  const subs = await operator.json("/api/admin/subscriptions");
  assertEquals(subs[0].user_email, ALICE);
  const audit = await operator.json("/api/admin/audit");
  const actions = new Set(audit.map((a: Json) => a.action));
  assert(actions.has("tenant.discovered") && actions.has("source.linked"));
  assertEquals((await operator.json("/api/admin/errors")).length, 0);
  assertEquals((await operator.get("/api/admin/errors?limit=501")).status, 422);
  assertEquals((await alice.get("/api/admin/tenants")).status, 403);

  // The admin-consent era endpoints are gone.
  assert([404, 405].includes((await operator.post("/api/admin/tenants", { display_name: "X" })).status));
  assertEquals((await operator.get(`/api/admin/tenants/${tenant.id}/consent-link`)).status, 404);
  assertEquals((await operator.post(`/api/admin/tenants/${tenant.id}/revoke`)).status, 404);
  assertEquals((await operator.get("/auth/adminconsent/callback")).status, 404);

  // Paused sources are counted per tenant and in the overview.
  await h.mock.revoke(ALICE);
  await h.settle();
  [tenant] = await operator.json("/api/admin/tenants");
  assertEquals([tenant.reauth_required_count, tenant.active_source_count], [1, 0]);
  overview = await operator.json("/api/admin/overview");
  assertEquals(overview.sources, { reauth_required: 1 });
  const errors = await operator.json(`/api/admin/errors?tenant_pk=${tenant.id}`);
  assertEquals(errors.map((e: Json) => e.kind), ["source.reauth_required"]);
});

testWithUsers("operators manage users", async (h, { alice, operator }) => {
  await h.createSourceEvent(ALICE, "Contoso event");
  await h.createSourceEvent("alice@fabrikam.example", "Fabrikam event");
  await h.linkAndSync(alice, ALICE);
  await h.linkAndSync(alice, "alice@fabrikam.example");

  const users = Object.fromEntries((await operator.json("/api/admin/users")).map((u: Json) => [u.email, u]));
  assertEquals(
    Object.keys(users[ALICE]).sort(),
    ["created_at", "display_name", "email", "event_count", "id", "is_operator", "source_count"],
  );
  assertEquals([users[ALICE].source_count, users[ALICE].event_count], [2, 2]);
  assertEquals([users["ops@calsync.example"].event_count, users["ops@calsync.example"].is_operator], [0, true]);

  const resp = await operator.patch(`/api/admin/users/${users[ALICE].id}`, { is_operator: true });
  assertEquals([resp.status, await resp.json()], [202, { status: "updated" }]);
  assertEquals((await alice.json("/api/me")).is_operator, true);
  const opsId = users["ops@calsync.example"].id;
  assertEquals((await operator.patch(`/api/admin/users/${opsId}`, { is_operator: false })).status, 400);
  assertEquals((await alice.get("/api/admin/users")).status, 200);

  // The primary-calendar endpoints are gone.
  assertEquals((await operator.post(`/api/admin/users/${users[ALICE].id}/provision`)).status, 404);
  assertEquals((await operator.post(`/api/admin/users/${users[ALICE].id}/rebuild`)).status, 404);
});
