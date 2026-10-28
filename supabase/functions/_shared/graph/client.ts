// Microsoft Graph client for source calendars: delegated tokens per linked source,
// per-tenant throttling and a hard read-only guard (FR-11).

import type { Settings } from "../config.ts";
import type { Sql } from "../db.ts";
import { httpFetch } from "../http.ts";
import { isoformatZ } from "../timeutil.ts";
import type { TokenService } from "./tokens.ts";

const DEFAULT_RETRY_AFTER = 10;
const PREFER_UTC = 'outlook.timezone="UTC"';

export class GraphError extends Error {
  // The linked source whose delegated token made the call.
  sourceId: string | null = null;

  constructor(
    public readonly status: number,
    public readonly code: string | null,
    public readonly graphMessage: string,
  ) {
    super(`Graph ${status} ${code ?? ""}: ${graphMessage}`.replace("  ", " ").trim());
  }

  get transient(): boolean {
    return this.status >= 500;
  }

  /** 401 even with a freshly refreshed token, or 403: the delegated grant no longer lets
   * this user read their calendar (revoked, consent withdrawn, policy change). */
  get authFailure(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

export class GraphNotFound extends GraphError {}
/** 410: delta token expired / resync required. */
export class GraphGone extends GraphError {}
export class GraphForbidden extends GraphError {}
export class GraphUnauthorized extends GraphError {}

export class GraphThrottled extends Error {
  constructor(
    public readonly scope: string,
    public readonly retryAfter: number,
  ) {
    super(`Throttled on ${scope}; retry after ${retryAfter.toFixed(1)}s`);
  }
}

/** Raised if code ever attempts to write to a source tenant (FR-11). */
export class ReadOnlyViolation extends Error {}

export function parseRetryAfter(value: string | null): number {
  if (!value) return DEFAULT_RETRY_AFTER;
  const seconds = Number(value);
  if (value.trim() !== "" && Number.isFinite(seconds)) return Math.max(seconds, 1);
  const date = Date.parse(value);
  if (!Number.isNaN(date)) return Math.max((date - Date.now()) / 1000, 1);
  return DEFAULT_RETRY_AFTER;
}

function isSubscriptionPath(path: string): boolean {
  return path.replace(/^\/+/, "").toLowerCase().startsWith("subscriptions");
}

/** Python's urllib.parse.quote: everything but unreserved characters and `safe`. */
export function quote(value: string, safe = ""): string {
  return [...value]
    .map((ch) =>
      /[A-Za-z0-9_.~-]/.test(ch) || safe.includes(ch)
        ? ch
        : [...new TextEncoder().encode(ch)].map((b) => `%${b.toString(16).toUpperCase().padStart(2, "0")}`).join("")
    )
    .join("");
}

// deno-lint-ignore no-explicit-any
export type GraphJson = Record<string, any>;

interface RequestOptions {
  sourceId: string;
  tenantId: string;
  json?: unknown;
  headers?: Record<string, string>;
}

export class GraphClient {
  constructor(
    readonly settings: Settings,
    private readonly sql: Sql,
    private readonly tokens: TokenService,
  ) {}

  source(sourceId: string, tenantId: string): SourceGraph {
    return new SourceGraph(this, sourceId, tenantId);
  }

  // -- throttling (per customer tenant) -----------------------------------------------------

  /** Seconds until the tenant may call Graph again (0 when not throttled). */
  async throttledFor(tenantId: string): Promise<number> {
    const [row] = await this.sql<{ wait: number }[]>`
      select greatest(extract(epoch from throttled_until - now()), 0)::float8 as wait
      from tenants where tenant_id = ${tenantId}
    `;
    return row?.wait ?? 0;
  }

  private async throttle(tenantId: string, seconds: number): Promise<void> {
    await this.sql`
      update tenants set throttled_until = now() + make_interval(secs => ${seconds})
      where tenant_id = ${tenantId}
    `;
  }

  // -- core request ---------------------------------------------------------------------------

  async request(method: string, path: string, opts: RequestOptions): Promise<GraphJson | null> {
    method = method.toUpperCase();
    if (method !== "GET" && !isSubscriptionPath(path)) {
      throw new ReadOnlyViolation(`Refusing ${method} ${path}: source calendars are read-only`);
    }
    const scope = `tenant:${opts.tenantId}`;
    const wait = await this.throttledFor(opts.tenantId);
    if (wait) throw new GraphThrottled(scope, wait);

    const url = path.startsWith("http") ? path : `${this.settings.graphBaseUrl}/${path.replace(/^\/+/, "")}`;

    let resp!: Response;
    for (let attempt = 0; attempt < 2; attempt++) {
      const token = await this.tokens.getSourceToken(opts.sourceId);
      const headers: Record<string, string> = { Authorization: `Bearer ${token}`, ...opts.headers };
      if (opts.json !== undefined) headers["Content-Type"] = "application/json";
      resp = await httpFetch(url, {
        method,
        headers,
        body: opts.json === undefined ? undefined : JSON.stringify(opts.json),
      });
      if (resp.status === 401 && attempt === 0) {
        // Expired or revoked access token: drop it and refresh once.
        await resp.body?.cancel();
        await this.tokens.invalidateSource(opts.sourceId);
        continue;
      }
      break;
    }

    if ([429, 503, 504].includes(resp.status)) {
      await resp.body?.cancel();
      const retryAfter = parseRetryAfter(resp.headers.get("Retry-After"));
      await this.throttle(opts.tenantId, retryAfter);
      console.info(`graph throttled scope=${scope} status=${resp.status} retry_after=${retryAfter.toFixed(1)}`);
      throw new GraphThrottled(scope, retryAfter);
    }
    const text = await resp.text();
    if (resp.status >= 400) {
      let code: string | null = null;
      let message = text.slice(0, 500);
      try {
        const err = JSON.parse(text).error ?? {};
        code = err.code ?? null;
        message = err.message ?? message;
      } catch {
        // not JSON
      }
      const cls = { 401: GraphUnauthorized, 403: GraphForbidden, 404: GraphNotFound, 410: GraphGone }[resp.status] ??
        GraphError;
      const error = new cls(resp.status, code, message);
      error.sourceId = opts.sourceId;
      throw error;
    }
    if (resp.status === 204 || !text) return null;
    return JSON.parse(text);
  }
}

function userPath(userId: string): string {
  return `users/${quote(userId, "@")}`;
}

export function calendarBase(userId: string, calendarId: string | null): string {
  const base = userPath(userId);
  return calendarId ? `${base}/calendars/${quote(calendarId)}` : `${base}/calendar`;
}

/** Read-only access to one linked source mailbox, with its owner's delegated
 * Calendars.Read token. Throttling is still tracked per customer tenant. */
export class SourceGraph {
  constructor(
    readonly client: GraphClient,
    readonly sourceId: string,
    readonly tenantId: string,
  ) {}

  private call(method: string, path: string, extra: Omit<RequestOptions, "sourceId" | "tenantId"> = {}) {
    return this.client.request(method, path, { sourceId: this.sourceId, tenantId: this.tenantId, ...extra });
  }

  async getEvent(userId: string, eventId: string): Promise<GraphJson> {
    return (await this.call("GET", `${userPath(userId)}/events/${quote(eventId)}`, {
      headers: { Prefer: PREFER_UTC },
    }))!;
  }

  async getDefaultCalendar(userId: string): Promise<GraphJson> {
    return (await this.call("GET", `${userPath(userId)}/calendar`))!;
  }

  deltaStartUrl(userId: string, calendarId: string | null, start: Date, end: Date): string {
    const base = calendarId ? calendarBase(userId, calendarId) : userPath(userId);
    const query = new URLSearchParams({ startDateTime: isoformatZ(start), endDateTime: isoformatZ(end) });
    return `${this.client.settings.graphBaseUrl}/${base}/calendarView/delta?${query}`;
  }

  async deltaPage(url: string, pageSize = 100): Promise<GraphJson> {
    return (await this.call("GET", url, { headers: { Prefer: `${PREFER_UTC}, odata.maxpagesize=${pageSize}` } }))!;
  }

  subscriptionResource(userId: string, calendarId: string | null): string {
    return calendarId ? `users/${userId}/calendars/${calendarId}/events` : `users/${userId}/events`;
  }

  async createSubscription(body: GraphJson): Promise<GraphJson> {
    return (await this.call("POST", "subscriptions", { json: body }))!;
  }

  async renewSubscription(subscriptionId: string, expires: Date): Promise<GraphJson | null> {
    return await this.call("PATCH", `subscriptions/${subscriptionId}`, {
      json: { expirationDateTime: isoformatZ(expires) },
    });
  }

  async reauthorizeSubscription(subscriptionId: string): Promise<void> {
    await this.call("POST", `subscriptions/${subscriptionId}/reauthorize`);
  }

  async deleteSubscription(subscriptionId: string): Promise<void> {
    await this.call("DELETE", `subscriptions/${subscriptionId}`);
  }
}
