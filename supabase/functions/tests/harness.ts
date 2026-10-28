// Test harness: the real Hono apps and sync code, in-process, against a dedicated
// `calsync_test` database on the local Supabase Postgres and the throwaway mock Microsoft
// 365 (`mockgraph-test`, http://localhost:8002). The mock delivers webhooks over HTTP to a
// server this process runs; work handed to `background` is tracked so tests can wait for
// it to settle (the equivalent of running the queued jobs before).
//
// Run with `make test` (starts the test mock; needs `supabase start`).

import { createApiApp } from "../_shared/api/app.ts";
import { createCronApp } from "../_shared/api/cron.ts";
import { createWebhooksApp } from "../_shared/api/webhooks.ts";
import { loadSettings, type Settings } from "../_shared/config.ts";
import { createSql, type SourceRow, type Sql } from "../_shared/db.ts";
import { createServices, type Services } from "../_shared/services.ts";
import { sweep, type SweepSummary } from "../_shared/sync/sweeps.ts";

export const SOURCE_APP = "22222222-2222-4222-8222-222222222222";
export const SOURCE_SECRET = "mock-source-secret";

const env = (key: string, fallback: string) => Deno.env.get(key) ?? fallback;
const ADMIN_DB_URL = env("TEST_ADMIN_DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres");
const DB_URL = env("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/calsync_test");
export const MOCK_URL = env("TEST_MOCK_URL", "http://localhost:8002");
const WEBHOOK_PORT = Number(env("TEST_WEBHOOK_PORT", "8090"));
// How the mock (a container) reaches this process's webhook server.
const PUBLIC_API_URL = env("TEST_PUBLIC_API_URL", `http://host.docker.internal:${WEBHOOK_PORT}`);
export const WEB = "http://web.test";

const TABLES = ["audit_log", "sync_errors", "source_events", "subscriptions", "linked_sources", "tenants", "users"];

// --- database ---------------------------------------------------------------------------

let sharedSql: Sql | undefined;

async function testDatabase(): Promise<Sql> {
  if (sharedSql) return sharedSql;
  const admin = createSql(ADMIN_DB_URL, 1);
  const name = new URL(DB_URL).pathname.slice(1);
  const [exists] = await admin`select 1 from pg_database where datname = ${name}`;
  if (!exists) await admin.unsafe(`create database "${name}"`);
  await admin.end();

  // Rebuilt once per run from the migration, so schema changes are always picked up. The
  // sweep schedule migration (pg_cron) only applies to the main database.
  const sql = createSql(DB_URL, 10);
  // A fresh `public` with a hosted project's defaults, where the Data API roles get every
  // new table unless the migration revokes it.
  await sql.unsafe(`
    drop schema if exists public cascade;
    create schema public;
    grant usage on schema public to anon, authenticated, service_role;
    alter default privileges for role postgres in schema public
      grant all on tables to anon, authenticated, service_role;
  `);
  const migration = new URL("../../migrations/20261025000000_initial_schema.sql", import.meta.url);
  await sql.unsafe(await Deno.readTextFile(migration));
  sharedSql = sql;
  return sql;
}

// --- settings -------------------------------------------------------------------------------

export function testSettings(overrides: Record<string, string> = {}): Settings {
  return loadSettings({
    DATABASE_URL: DB_URL,
    PUBLIC_API_URL,
    PUBLIC_WEB_URL: WEB,
    LOGIN_BASE_URL: MOCK_URL,
    GRAPH_BASE_URL: `${MOCK_URL}/v1.0`,
    SESSION_SECRET: "test-session-secret-" + "x".repeat(40),
    DATA_ENCRYPTION_KEYS: btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32))))
      .replace(/\+/g, "-")
      .replace(/\//g, "_"),
    SOURCE_APP_CLIENT_ID: SOURCE_APP,
    SOURCE_APP_CLIENT_SECRET: SOURCE_SECRET,
    OPERATOR_EMAILS: "ops@calsync.example",
    CRON_SECRET: "test-cron-secret",
    ...overrides,
  });
}

// --- mock Microsoft 365 -------------------------------------------------------------------

// deno-lint-ignore no-explicit-any
export type Json = any;

export interface MockUser {
  oid: string;
  tenant_id: string;
  default_calendar_id: string;
  session_generation: number;
}

export class Mock {
  constructor(readonly url: string) {}

  async call(method: string, path: string, body?: unknown): Promise<Json> {
    const resp = await fetch(`${this.url}${path}`, {
      method,
      headers: body === undefined ? {} : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await resp.text();
    if (!resp.ok) throw new Error(`mock ${method} ${path}: ${resp.status} ${text}`);
    return text ? JSON.parse(text) : null;
  }

  reset() {
    return this.call("POST", "/mock/api/reset", {});
  }
  state() {
    return this.call("GET", "/mock/api/state");
  }
  /** Wait until every notification sent so far has been delivered. */
  drain() {
    return this.call("POST", "/mock/api/drain");
  }
  async user(upn: string): Promise<MockUser> {
    const user = (await this.state()).users[upn];
    if (!user) throw new Error(`no mock user ${upn}`);
    return user;
  }
  async tenantOf(upn: string): Promise<string> {
    return (await this.user(upn)).tenant_id;
  }
  async subscriptions(): Promise<Json[]> {
    return (await this.state()).subscriptions;
  }
  async events(upn: string): Promise<Json[]> {
    return (await this.state()).events[upn] ?? [];
  }
  createEvent(upn: string, event: Json, icalUid?: string) {
    return this.call("POST", "/mock/api/events", { upn, event, ical_uid: icalUid });
  }
  updateEvent(upn: string, eventId: string, patch: Json) {
    return this.call("PATCH", `/mock/api/events/${upn}/${eventId}`, patch);
  }
  deleteEvent(upn: string, eventId: string) {
    return this.call("DELETE", `/mock/api/events/${upn}/${eventId}`);
  }
  revoke(upn: string, opts: { withdraw_consent?: boolean; notify?: boolean } = {}) {
    return this.call("POST", `/mock/api/users/${upn}/revoke`, opts);
  }
  expireRefreshTokens(upn: string) {
    return this.call("POST", `/mock/api/users/${upn}/expire-refresh-tokens`);
  }
  tenant(
    ref: string,
    flags: { user_consent_blocked?: boolean; drop_notifications?: boolean; throttle_remaining?: number },
  ) {
    return this.call("POST", `/mock/api/tenants/${ref}`, flags);
  }
  lifecycle(ref: string, event: string) {
    return this.call("POST", `/mock/api/tenants/${ref}/lifecycle`, { event });
  }
  forgetSubscriptions() {
    return this.call("POST", "/mock/api/subscriptions/forget");
  }
  config(values: { max_page_size?: number }) {
    return this.call("POST", "/mock/api/config", values);
  }
  async sign(claims: Json): Promise<string> {
    return (await this.call("POST", "/mock/api/sign", { claims })).token;
  }
  verify(token: string): Promise<Json> {
    return this.call("POST", "/mock/api/verify", { token });
  }
}

// --- browser ------------------------------------------------------------------------------

/** A browser on the web app's origin: a cookie jar and the CSRF header the app sends.
 * Requests go straight to the in-process app (what the Next.js proxy forwards to). */
export class Browser {
  private cookies = new Map<string, { value: string; path: string }>();

  constructor(
    private readonly h: Harness,
    private readonly csrf = true,
  ) {}

  cookie(name: string): string | undefined {
    return this.cookies.get(name)?.value;
  }

  /** A browser with the same cookies but no CSRF header. */
  withoutCsrf(): Browser {
    const other = new Browser(this.h, false);
    other.cookies = new Map(this.cookies);
    return other;
  }

  private store(resp: Response) {
    for (const header of resp.headers.getSetCookie()) {
      const [pair, ...attrs] = header.split(";").map((p) => p.trim());
      const eq = pair.indexOf("=");
      const name = pair.slice(0, eq);
      const value = pair.slice(eq + 1);
      const attr = (key: string) => attrs.find((a) => a.toLowerCase().startsWith(`${key}=`))?.slice(key.length + 1);
      if (attr("max-age") === "0" || value === "") this.cookies.delete(name);
      else this.cookies.set(name, { value, path: attr("path") ?? "/" });
    }
  }

  async request(method: string, path: string, body?: unknown): Promise<Response> {
    const url = new URL(path, WEB);
    const headers = new Headers();
    if (this.csrf) headers.set("x-calsync-csrf", "1");
    const jar = [...this.cookies].filter(([, c]) => url.pathname.startsWith(c.path));
    if (jar.length) headers.set("cookie", jar.map(([n, c]) => `${n}=${c.value}`).join("; "));
    if (body !== undefined) headers.set("content-type", "application/json");
    const resp = await this.h.api.request(url.pathname + url.search, {
      method,
      headers,
      body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
    });
    this.store(resp);
    return resp;
  }

  get(path: string) {
    return this.request("GET", path);
  }
  post(path: string, body?: unknown) {
    return this.request("POST", path, body);
  }
  patch(path: string, body: unknown) {
    return this.request("PATCH", path, body);
  }
  delete(path: string) {
    return this.request("DELETE", path);
  }
  async json(path: string): Promise<Json> {
    const resp = await this.get(path);
    if (resp.status !== 200) throw new Error(`GET ${path}: ${resp.status} ${await resp.text()}`);
    return await resp.json();
  }
}

// --- harness --------------------------------------------------------------------------------

let current: Harness | undefined;
let server: Deno.HttpServer | undefined;

function webhookServer() {
  server ??= Deno.serve({ port: WEBHOOK_PORT, hostname: "0.0.0.0", onListen: () => {} }, (req) => {
    if (!current) return new Response("no harness", { status: 503 });
    const path = new URL(req.url).pathname;
    if (path.startsWith("/webhooks/")) return current.webhooks.fetch(req);
    return new Response("not found", { status: 404 });
  });
}

export class Harness {
  readonly mock = new Mock(MOCK_URL);
  readonly pending = new Set<Promise<unknown>>();
  /** How many background tasks were started (webhook processing, setups, unlinks...). */
  started = 0;
  api!: ReturnType<typeof createApiApp>;
  webhooks!: ReturnType<typeof createWebhooksApp>;
  cron!: ReturnType<typeof createCronApp>;
  services!: Services;
  sql!: Sql;

  private constructor(readonly settings: Settings) {}

  static async create(overrides: Record<string, string> = {}): Promise<Harness> {
    const h = new Harness(testSettings(overrides));
    h.sql = await testDatabase();
    await h.sql.unsafe(`truncate ${TABLES.join(", ")} cascade`);
    await h.mock.reset();
    h.services = await createServices(h.settings, { sql: h.sql, background: (task) => h.track(task) });
    h.api = createApiApp(h.services);
    h.webhooks = createWebhooksApp(h.services);
    h.cron = createCronApp(h.services);
    current = h;
    webhookServer();
    return h;
  }

  /** Services that share this harness's database and background tracking but use other
   * settings (e.g. an unreachable identity platform). */
  async servicesWith(overrides: Record<string, string>): Promise<Services> {
    const settings = { ...testSettings(overrides), encryptionKeys: this.settings.encryptionKeys };
    return await createServices(settings, { sql: this.sql, background: (task) => this.track(task) });
  }

  private track(task: Promise<unknown>) {
    this.started++;
    const p: Promise<unknown> = task
      .catch((err) => console.error("background task failed", err))
      .finally(() => this.pending.delete(p));
    this.pending.add(p);
  }

  /** Wait until the mock has delivered every notification and all background work they
   * (or the requests so far) started has finished. */
  async settle(): Promise<void> {
    for (let i = 0; i < 50; i++) {
      await this.mock.drain();
      if (!this.pending.size) {
        await this.mock.drain();
        if (!this.pending.size) return;
      }
      await Promise.allSettled([...this.pending]);
    }
    throw new Error("background work did not settle");
  }

  async close(): Promise<void> {
    await this.settle();
    if (current === this) current = undefined;
  }

  // -- flows -----------------------------------------------------------------------------

  browser(): Browser {
    return new Browser(this);
  }

  /** Act as the mock's sign-in page: pick `upn` for the authorize URL `location`. */
  async authorize(location: string, upn: string): Promise<Response> {
    const url = new URL(location);
    if (url.origin !== new URL(MOCK_URL).origin) throw new Error(`unexpected authorize URL ${location}`);
    const tenantPath = url.pathname.replace(/^\/+/, "").split("/")[0];
    const user = await this.mock.user(upn);
    const params = new URLSearchParams(url.search);
    params.set("oid", user.oid);
    params.set("tenant_path", tenantPath);
    return await fetch(`${MOCK_URL}/mock/authorize/complete?${params}`, { redirect: "manual" });
  }

  async oauthRoundtrip(browser: Browser, startPath: string, upn: string): Promise<Response> {
    const start = await browser.get(startPath);
    if (start.status !== 302) throw new Error(`${startPath}: ${start.status} ${await start.text()}`);
    const authorized = await this.authorize(start.headers.get("location")!, upn);
    await authorized.body?.cancel();
    if (authorized.status !== 302) throw new Error(`authorize: ${authorized.status}`);
    const callback = new URL(authorized.headers.get("location")!);
    return await browser.get(callback.pathname + callback.search);
  }

  async signIn(upn: string): Promise<Browser> {
    const browser = this.browser();
    const resp = await this.oauthRoundtrip(browser, "/auth/login", upn);
    if (resp.status !== 302 || !browser.cookie("cs_session")) throw new Error(`sign-in failed: ${resp.status}`);
    return browser;
  }

  static resultOf(resp: Response): Record<string, string> {
    if (resp.status !== 302 && resp.status !== 307) throw new Error(`expected a redirect, got ${resp.status}`);
    const location = resp.headers.get("location")!;
    if (!location.startsWith(`${WEB}/link/result?`)) throw new Error(`unexpected redirect ${location}`);
    return Object.fromEntries(new URL(location).searchParams);
  }

  /** Link (or re-link) a source by signing in as `upn`. Returns the result query. */
  async link(browser: Browser, upn: string): Promise<Record<string, string>> {
    return Harness.resultOf(await this.oauthRoundtrip(browser, "/auth/link", upn));
  }

  /** Link and wait for the subscription and backfill. */
  async linkAndSync(browser: Browser, upn: string): Promise<Record<string, string>> {
    const result = await this.link(browser, upn);
    await this.settle();
    return result;
  }

  /** Reconnect a source, signing in as `upn` on the source tenant's login page. */
  async reauth(browser: Browser, sourceId: string, upn: string): Promise<Record<string, string>> {
    return Harness.resultOf(await this.oauthRoundtrip(browser, `/auth/reauth/${sourceId}`, upn));
  }

  // -- inspection ------------------------------------------------------------------------

  /** The user's unified calendar from yesterday to `days` ahead (GET /api/calendar). */
  async calendar(browser: Browser, days = 60): Promise<Json[]> {
    const now = Date.now();
    const query = new URLSearchParams({
      start: new Date(now - 86_400_000).toISOString(),
      end: new Date(now + days * 86_400_000).toISOString(),
    });
    return await browser.json(`/api/calendar?${query}`);
  }

  async subjects(browser: Browser): Promise<string[]> {
    return (await this.calendar(browser)).map((e) => e.subject).sort();
  }

  async source(email: string): Promise<SourceRow & { refresh_token_plain: string | null }> {
    const [row] = await this.sql<SourceRow[]>`select * from linked_sources where source_email = ${email}`;
    if (!row) throw new Error(`no source ${email}`);
    return { ...row, refresh_token_plain: await this.services.cipher.decryptOptional(row.refresh_token) };
  }

  async subscriptionIds(): Promise<string[]> {
    const rows = await this.sql<{ graph_subscription_id: string }[]>`select graph_subscription_id from subscriptions`;
    return rows.map((r) => r.graph_subscription_id);
  }

  async sourceEventCount(): Promise<number> {
    const [row] = await this.sql<{ n: number }[]>`select count(*)::int as n from source_events`;
    return row.n;
  }

  async createSourceEvent(
    upn: string,
    subject: string,
    { hoursFromNow = 24, icalUid, ...extra }: { hoursFromNow?: number; icalUid?: string; [key: string]: unknown } = {},
  ): Promise<Json> {
    const start = new Date(Date.now() + hoursFromNow * 3_600_000);
    start.setUTCMinutes(0, 0, 0);
    const fmt = (d: Date) => d.toISOString().slice(0, 19);
    return await this.mock.createEvent(
      upn,
      {
        subject,
        start: { dateTime: fmt(start), timeZone: "UTC" },
        end: { dateTime: fmt(new Date(start.getTime() + 3_600_000)), timeZone: "UTC" },
        ...extra,
      },
      icalUid,
    );
  }

  /** Run the periodic sweep and wait for everything it started. */
  async sweep(): Promise<SweepSummary> {
    const summary = await sweep(this.services);
    await this.settle();
    return summary;
  }
}

/** Deno.test with a fresh harness and the resource sanitizers off (the harness keeps a
 * database pool and the webhook server open across tests). */
export function test(name: string, fn: (h: Harness) => Promise<void>) {
  Deno.test({
    name,
    sanitizeOps: false,
    sanitizeResources: false,
    async fn() {
      const h = await Harness.create();
      try {
        await fn(h);
      } finally {
        await h.close();
      }
    },
  });
}

/** A harness with Alice (and the operator) signed in, nothing linked yet. */
export function testWithUsers(
  name: string,
  fn: (h: Harness, users: { alice: Browser; operator: Browser }) => Promise<void>,
) {
  test(name, async (h) => {
    const operator = await h.signIn("ops@calsync.example");
    const alice = await h.signIn("alice@contoso.example");
    await fn(h, { alice, operator });
  });
}
