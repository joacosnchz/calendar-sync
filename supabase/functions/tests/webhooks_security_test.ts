// Webhook receiver contract, Graph client guards, delegated tokens, cron and session
// security, and the database being closed to Supabase's Data API.

import { assert, assertEquals, assertNotEquals, assertRejects } from "jsr:@std/assert@1.0.13";
import { GraphForbidden, GraphNotFound, GraphThrottled, ReadOnlyViolation } from "../_shared/graph/client.ts";
import { TokenError } from "../_shared/graph/tokens.ts";
import { transaction } from "../_shared/db.ts";
import { issueOAuthState } from "../_shared/security.ts";
import { runTask } from "../_shared/sync/runner.ts";
import { describeSync, requestResync, syncSource } from "../_shared/sync/source_sync.ts";
import {
  type Browser,
  type Harness,
  Harness as H,
  type Json,
  MOCK_URL,
  SOURCE_APP,
  SOURCE_SECRET,
  test,
  testWithUsers,
} from "./harness.ts";

const ALICE = "alice@contoso.example";

async function post(h: Harness, path: string, body: unknown): Promise<Response> {
  return await h.webhooks.request(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

async function subscribed(h: Harness, alice: Browser): Promise<Json> {
  await h.linkAndSync(alice, ALICE);
  return (await h.mock.subscriptions())[0];
}

function payload(sub: Json, overrides: Record<string, unknown> = {}) {
  return {
    value: [{
      subscriptionId: sub.id,
      changeType: "created",
      resource: `Users/${sub.owner_oid}/Events/AAMkX`,
      resourceData: { id: "AAMkX" },
      clientState: sub.client_state,
      tenantId: sub.tenant_id,
      ...overrides,
    }],
  };
}

// --- webhook receiver -----------------------------------------------------------------------

for (const path of ["/webhooks/graph", "/webhooks/lifecycle"]) {
  test(`validation handshake echoes the token (${path})`, async (h) => {
    const resp = await h.webhooks.request(`${path}?validationToken=abc%20123%3C`, { method: "POST" });
    assertEquals(resp.status, 200);
    assert(resp.headers.get("content-type")?.startsWith("text/plain"));
    assertEquals(await resp.text(), "abc 123<");
  });
}

testWithUsers("a wrong clientState is acknowledged but dropped", async (h, { alice }) => {
  const sub = await subscribed(h, alice);
  const started = h.started;
  const resp = await post(h, "/webhooks/graph", payload(sub, { clientState: "forged" }));
  assertEquals(resp.status, 202); // acknowledge so Graph doesn't retry, but drop it
  assertEquals(h.started, started);
});

testWithUsers("a missing clientState or unknown subscription is dropped", async (h, { alice }) => {
  const sub = await subscribed(h, alice);
  const started = h.started;
  await post(h, "/webhooks/graph", payload(sub, { clientState: null }));
  await post(h, "/webhooks/graph", payload(sub, { subscriptionId: crypto.randomUUID() }));
  assertEquals(h.started, started);
});

testWithUsers("a tenant mismatch is dropped", async (h, { alice }) => {
  const sub = await subscribed(h, alice);
  const started = h.started;
  await post(h, "/webhooks/graph", payload(sub, { tenantId: crypto.randomUUID() }));
  assertEquals(h.started, started);
});

testWithUsers("a valid notification is acknowledged first and processed after", async (h, { alice }) => {
  const sub = await subscribed(h, alice);
  const started = h.started;
  const resp = await post(h, "/webhooks/graph", payload(sub));
  assertEquals(resp.status, 202);
  assertEquals(h.started, started + 1);
  assertEquals(h.pending.size, 1); // still running when the response was sent
  await h.settle();
  // Unknown event id: nothing stored, the source's last notification is recorded.
  assertEquals(await h.sourceEventCount(), 0);
  assert((await h.source(ALICE)).last_notification_at !== null);
});

test("a malformed body is rejected", async (h) => {
  const resp = await post(h, "/webhooks/graph", "not json");
  assertEquals(resp.status, 400);
  assertEquals((await post(h, "/webhooks/graph", { value: "nope" })).status, 400);
});

testWithUsers("lifecycle notifications need the clientState too", async (h, { alice }) => {
  const sub = await subscribed(h, alice);
  const started = h.started;
  const body = (clientState: string) => ({
    value: [{ subscriptionId: sub.id, lifecycleEvent: "missed", clientState, tenantId: sub.tenant_id }],
  });
  await post(h, "/webhooks/lifecycle", body("forged"));
  assertEquals(h.started, started);
  await post(h, "/webhooks/lifecycle", body(sub.client_state));
  assertEquals(h.started, started + 1);
});

// --- Graph client ---------------------------------------------------------------------------

testWithUsers("the source app cannot write (FR-11)", async (h, { alice }) => {
  await subscribed(h, alice);
  const tenantId = await h.mock.tenantOf(ALICE);
  const source = await h.source(ALICE);
  for (const method of ["POST", "PATCH", "DELETE", "PUT"]) {
    await assertRejects(
      () => h.services.graph.request(method, "users/x/events/y", { sourceId: source.id, tenantId, json: {} }),
      ReadOnlyViolation,
    );
  }
  // Reads and subscription management are allowed.
  await assertRejects(
    () => h.services.graph.source(source.id, tenantId).deleteSubscription(crypto.randomUUID()),
    GraphNotFound,
  );
});

test("the source app cannot use client credentials", async (h) => {
  // The token service has no app-only tokens at all...
  assert(!("getToken" in h.services.tokens));
  // ...and the identity platform refuses them: the app has no application permissions.
  for (const upn of [ALICE, "ops@calsync.example"]) {
    const resp = await fetch(`${MOCK_URL}/${await h.mock.tenantOf(upn)}/oauth2/v2.0/token`, {
      method: "POST",
      body: new URLSearchParams({
        grant_type: "client_credentials",
        client_id: SOURCE_APP,
        client_secret: SOURCE_SECRET,
        scope: "https://graph.microsoft.com/.default",
      }),
    });
    assertEquals(resp.status, 400);
    assertEquals((await resp.json()).error, "unauthorized_client");
  }
});

testWithUsers("delegated tokens are cached per source", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  await h.linkAndSync(alice, "alice@fabrikam.example");
  const contoso = await h.source(ALICE);
  const fabrikam = await h.source("alice@fabrikam.example");
  const tokens = h.services.tokens;
  const a1 = await tokens.getSourceToken(contoso.id);
  assertEquals(await tokens.getSourceToken(contoso.id), a1);
  assertNotEquals(await tokens.getSourceToken(fabrikam.id), a1);
  const cached = await h.source(ALICE);
  assert(cached.access_token_expires_at!.getTime() > Date.now() + 3000_000);
  assertNotEquals(cached.access_token, a1); // encrypted at rest
});

testWithUsers("access tokens refresh once and rotation is persisted", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const source = await h.source(ALICE);
  const tokens = h.services.tokens;
  await tokens.invalidateSource(source.id);

  // Several instances need a token at once: one refresh, everyone gets its result. (The
  // mock rotates strictly, so a second redemption of the same refresh token would fail.)
  const results = await Promise.all([1, 2, 3, 4, 5].map(() => tokens.getSourceToken(source.id)));
  assertEquals(new Set(results).size, 1);
  const rotated = await h.source(ALICE);
  assertNotEquals(rotated.refresh_token_plain, source.refresh_token_plain);
  const live = (await h.mock.state()).refresh_tokens;
  assert(!live.includes(source.refresh_token_plain) && live.includes(rotated.refresh_token_plain));
  assert(rotated.granted_scopes?.includes("Calendars.Read"));

  // The refresh went to the source's own tenant endpoint, and the token reads Graph.
  const owner = await h.mock.user(ALICE);
  const claims = await h.mock.verify(results[0]);
  assertEquals([claims.kind, claims.oid], ["delegated", owner.oid]);
  assert(claims.scp.includes("Calendars.Read"));
  const graph = h.services.graph.source(source.id, owner.tenant_id);
  assertEquals((await graph.getDefaultCalendar(owner.oid)).id, owner.default_calendar_id);

  // A rejected (e.g. expired) access token is dropped and refreshed once, transparently.
  await tokens.cacheSourceToken(source.id, "garbage", 3600);
  assertEquals((await graph.getDefaultCalendar(owner.oid)).id, owner.default_calendar_id);
  assertNotEquals((await h.source(ALICE)).refresh_token_plain, rotated.refresh_token_plain);
});

for (const scenario of ["expired", "revoked"]) {
  testWithUsers(`a dead refresh token requires reauth (${scenario})`, async (h, { alice }) => {
    await h.linkAndSync(alice, ALICE);
    const source = await h.source(ALICE);
    if (scenario === "expired") await h.mock.expireRefreshTokens(ALICE);
    else await h.mock.revoke(ALICE, { notify: false });
    await h.services.tokens.invalidateSource(source.id);
    const err = await assertRejects(() => h.services.tokens.getSourceToken(source.id), TokenError);
    assert(err.reauthRequired);
    assertEquals([err.code, err.sourceId], ["AADSTS700082", source.id]);

    // Through the task runner: the source is paused, not failed, and nothing is retried.
    const outcome = await runTask(
      h.services,
      "sync_source",
      async () => describeSync(await syncSource(h.services, source.id, "scheduled")),
      { sourceId: source.id },
    );
    assertEquals(outcome, "reauth-required");
    const paused = await h.source(ALICE);
    assertEquals([paused.status, paused.resync_requested_at], ["reauth_required", null]);
  });
}

testWithUsers("transient token failures are retried, then left to the sweep", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const source = await h.source(ALICE);
  await h.services.tokens.invalidateSource(source.id);
  // The identity platform is unreachable.
  const offline = await h.servicesWith({ LOGIN_BASE_URL: "http://127.0.0.1:9" });
  const outcome = await runTask(
    offline,
    "sync_source",
    async () => describeSync(await syncSource(offline, source.id, "scheduled")),
    { sourceId: source.id, onTransient: () => requestResync(offline.sql, source.id) },
  );
  assertEquals(outcome, "retry-later");
  const after = await h.source(ALICE);
  assertEquals(after.status, "active");
  assert(after.resync_requested_at !== null);
  // Back online, the sweep catches up.
  assertEquals((await h.sweep()).delta, 1);
  assertEquals((await h.source(ALICE)).resync_requested_at, null);
});

const ADMIN_APPROVAL_CASES: [string, string | null, boolean][] = [
  ["access_denied", "AADSTS90094: The grant requires admin permission.", true],
  ["access_denied", "AADSTS90008: The user or administrator has not consented.", true],
  ["invalid_client", "AADSTS65001: ... Need admin approval", true],
  ["consent_required", null, true],
  ["access_denied", "AADSTS65004: User declined to consent.", false],
  ["access_denied", "AADSTS65001: The user has not consented", false],
];

test("authorize errors map to the right result page", async (h) => {
  for (const [error, description, expected] of ADMIN_APPROVAL_CASES) {
    const params = new URLSearchParams({ error, ...(description ? { error_description: description } : {}) });
    const result = H.resultOf(await h.browser().get(`/auth/callback?${params}`));
    assertEquals(result.status, expected ? "admin_approval_required" : "error");
    assert(result.message);
  }
});

testWithUsers("throttling is per tenant", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  await h.linkAndSync(alice, "alice@fabrikam.example");
  const contoso = await h.source(ALICE);
  const fabrikam = await h.source("alice@fabrikam.example");
  const contosoTid = await h.mock.tenantOf(ALICE);
  const fabrikamTid = await h.mock.tenantOf("alice@fabrikam.example");
  await h.mock.tenant(contosoTid, { throttle_remaining: 1 });
  const graph = h.services.graph;
  const err = await assertRejects(
    () => graph.source(contoso.id, contosoTid).getDefaultCalendar(ALICE),
    GraphThrottled,
  );
  assertEquals(err.retryAfter, 2);
  // Still backing off (without calling Graph), while other tenants are unaffected.
  await assertRejects(() => graph.source(contoso.id, contosoTid).getDefaultCalendar(ALICE), GraphThrottled);
  assert(await graph.source(fabrikam.id, fabrikamTid).getDefaultCalendar("alice@fabrikam.example"));
});

testWithUsers("a delegated token only reads its own mailbox", async (h, { alice }) => {
  await h.linkAndSync(alice, ALICE);
  const source = await h.source(ALICE);
  const graph = h.services.graph.source(source.id, await h.mock.tenantOf(ALICE));
  assert(await graph.getDefaultCalendar(ALICE));
  for (const other of ["bob@contoso.example", "alice@fabrikam.example"]) {
    await assertRejects(() => graph.getDefaultCalendar(other), GraphForbidden);
  }
});

// --- cron -----------------------------------------------------------------------------------

test("the sweep endpoint needs the cron secret", async (h) => {
  const call = (secret?: string) =>
    h.cron.request("/cron/sweep?wait=1", {
      method: "POST",
      headers: secret ? { "x-cron-secret": secret } : {},
    });
  assertEquals((await call()).status, 401);
  assertEquals((await call("wrong")).status, 401);
  const ok = await call("test-cron-secret");
  assertEquals(ok.status, 200);
  assertEquals((await ok.json()).renew, 0);

  // Without the wait flag (what pg_cron sends) it answers at once and sweeps after.
  const started = h.started;
  const resp = await h.cron.request("/cron/sweep", {
    method: "POST",
    headers: { "x-cron-secret": "test-cron-secret" },
  });
  assertEquals([resp.status, await resp.json()], [202, { status: "started" }]);
  assertEquals(h.started, started + 1);
});

// --- sessions, state, CSRF ------------------------------------------------------------------

testWithUsers("mutations need the CSRF header", async (_h, { alice }) => {
  const bare = alice.withoutCsrf();
  assertEquals((await bare.get("/api/me")).status, 200);
  assertEquals((await bare.delete("/api/me")).status, 403);
  assertEquals((await alice.json("/api/me")).email, ALICE); // still there
});

test("unauthenticated requests are refused", async (h) => {
  const browser = h.browser();
  assertEquals((await browser.get("/api/me")).status, 401);
  assertEquals((await browser.get("/api/admin/overview")).status, 401);
  assertEquals((await browser.get("/auth/link")).status, 401);
  assertEquals((await browser.get(`/auth/reauth/${crypto.randomUUID()}`)).status, 401);
  assertEquals((await h.api.request("/api/me", { headers: { cookie: "cs_session=forged" } })).status, 401);
});

test("sign-in rejects a forged state", async (h) => {
  const browser = h.browser();
  let resp = await browser.get("/auth/callback?code=x&state=forged");
  assertEquals(H.resultOf(resp).status, "error");
  // A valid state without the browser-binding cookie is rejected too.
  const state = await issueOAuthState(h.settings, { purpose: "login", nonce: "n" });
  resp = await browser.get(`/auth/callback?${new URLSearchParams({ code: "x", state })}`);
  assertEquals(H.resultOf(resp).status, "error");
});

testWithUsers("operators come from configuration", async (h, { alice, operator }) => {
  assertEquals((await operator.json("/api/me")).is_operator, true);
  const [user] = await h.sql`select id from users where email = ${ALICE}`;
  assertEquals(await alice.json("/api/me"), {
    id: user.id,
    email: ALICE,
    display_name: "Alice Martin",
    is_operator: false,
  });
});

test("signing in lands on the calendar", async (h) => {
  const browser = h.browser();
  let resp = await h.oauthRoundtrip(browser, "/auth/login", ALICE);
  assertEquals(resp.headers.get("location"), "http://web.test/calendar");
  const session = browser.cookie("cs_session");
  assert(session);
  assertEquals(browser.cookie("cs_oauth"), undefined); // single use
  resp = await h.oauthRoundtrip(browser, "/auth/login?next=/dashboard", ALICE);
  assertEquals(resp.headers.get("location"), "http://web.test/dashboard");
  // Signing up starts no background work: there is no calendar to provision.
  assertEquals(h.started, 0);
  const audit = await h.sql`select action from audit_log order by created_at`;
  assertEquals(audit.map((a) => a.action), ["user.signed_up", "user.signed_in"]);

  assertEquals((await browser.post("/auth/logout")).status, 204);
  assertEquals(browser.cookie("cs_session"), undefined);
});

// --- database ---------------------------------------------------------------------------------

test("tables are closed to the Data API roles", async (h) => {
  for (const role of ["anon", "authenticated"]) {
    for (const table of ["users", "linked_sources", "source_events", "subscriptions", "audit_log"]) {
      const err = await assertRejects(() =>
        transaction(h.sql, async (tx) => {
          await tx.unsafe(`set local role ${role}`);
          await tx.unsafe(`select * from ${table} limit 1`);
        })
      );
      assert(String((err as Error).message).includes("permission denied"), `${role} ${table}: ${err}`);
    }
  }
  const rls = await h.sql`
    select relname from pg_class
    where relnamespace = 'public'::regnamespace and relkind = 'r' and not relrowsecurity
  `;
  assertEquals(rls.map((r) => r.relname), []);
});
