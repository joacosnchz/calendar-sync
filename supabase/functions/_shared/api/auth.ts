// Sign-in, source account linking (FR-3) and reconnecting a source after its delegated
// grant stopped working.
//
// Every flow is a delegated OIDC round trip against the user's own tenant. Linking and
// reconnecting request Calendars.Read + offline_access, which each user consents to for
// their own mailbox; the refresh token that comes back is stored (encrypted) on the source.

import { audit } from "../audit.ts";
import { deleteCookie, getCookie, Hono, setCookie } from "../deps.ts";
import { SourceStatus, type Sql, type TenantRow, transaction, type UserRow } from "../db.ts";
import {
  authorizeUrl,
  canReadCalendars,
  type CodeExchange,
  exchangeCode,
  LINK_SCOPES,
  needsAdminApproval,
  OidcError,
  SIGN_IN_SCOPES,
  type SignedInIdentity,
} from "../graph/oidc.ts";
import {
  InvalidToken,
  issueOAuthState,
  issueSession,
  isUuid,
  newNonce,
  OAUTH_COOKIE,
  type OAuthState,
  readOAuthState,
  readSession,
  safeNextPath,
  SESSION_COOKIE,
} from "../security.ts";
import type { Services } from "../services.ts";
import { setupSource } from "../sync/lifecycle.ts";
import { runTask } from "../sync/runner.ts";
import { requestResync, SyncMode } from "../sync/source_sync.ts";
import { type AppEnv, type Ctx, currentUser, uuidParam } from "./common.ts";

const RESULT_PATH = "/link/result";

function web(services: Services, path: string, params: Record<string, string> = {}): string {
  const query = Object.keys(params).length ? `?${new URLSearchParams(params)}` : "";
  return `${services.settings.publicWebUrl}${path}${query}`;
}

function result(c: Ctx, status: string, params: Record<string, string> = {}): Response {
  return c.redirect(web(c.get("services"), RESULT_PATH, { status, ...params }), 302);
}

function error(c: Ctx, message: string): Response {
  return result(c, "error", { message });
}

interface ReauthSource {
  id: string;
  user_id: string;
  status: string;
  source_email: string;
  source_user_id: string;
  tenant_pk: string;
  tenant_id: string | null;
}

async function start(
  c: Ctx,
  opts: { purpose: OAuthState["purpose"]; user?: UserRow; nextPath?: string; source?: ReauthSource },
): Promise<Response> {
  const { settings } = c.get("services");
  const nonce = newNonce();
  const state = await issueOAuthState(settings, {
    purpose: opts.purpose,
    nonce,
    userId: opts.user?.id,
    nextPath: opts.nextPath ?? "/calendar",
    sourceId: opts.source?.id,
  });
  let url: string;
  if (opts.purpose === "login") {
    url = authorizeUrl(settings, { state, nonce, scopes: SIGN_IN_SCOPES });
  } else if (opts.source) {
    // Reconnect: straight to the source's own tenant, pre-filled with its account.
    url = authorizeUrl(settings, {
      state,
      nonce,
      scopes: LINK_SCOPES,
      tenant: opts.source.tenant_id || "organizations",
      loginHint: opts.source.source_email,
      prompt: null,
    });
  } else {
    url = authorizeUrl(settings, { state, nonce, scopes: LINK_SCOPES });
  }
  // Binds the OAuth round trip to this browser (login CSRF protection).
  setCookie(c, OAUTH_COOKIE, nonce, {
    maxAge: 900,
    httpOnly: true,
    sameSite: "Lax",
    secure: settings.cookiesSecure,
    path: "/auth",
  });
  return c.redirect(url, 302);
}

async function loadReauthSource(sql: Sql, sourceId: string): Promise<ReauthSource | undefined> {
  const [source] = await sql<ReauthSource[]>`
    select ls.id, ls.user_id, ls.status, ls.source_email, ls.source_user_id, ls.tenant_pk, t.tenant_id
    from linked_sources ls join tenants t on t.id = ls.tenant_pk
    where ls.id = ${sourceId}
  `;
  return source;
}

export function authRoutes(): Hono<AppEnv> {
  const app = new Hono<AppEnv>();

  app.get("/login", (c) => start(c, { purpose: "login", nextPath: safeNextPath(c.req.query("next")) }));

  app.get("/link", async (c) => start(c, { purpose: "link", user: await currentUser(c) }));

  app.get("/reauth/:id", async (c) => {
    const user = await currentUser(c);
    const sourceId = uuidParam(c, "id");
    const source = await loadReauthSource(c.get("services").sql, sourceId);
    // Isolation: only the owner can reconnect a source (and learns nothing about others).
    if (!source || source.user_id !== user.id) return error(c, "Calendar not found");
    if (source.status === SourceStatus.REMOVING) return error(c, "This calendar is being unlinked");
    return start(c, { purpose: "reauth", user, source });
  });

  app.post("/logout", (c) => {
    deleteCookie(c, SESSION_COOKIE, { path: "/" });
    return c.body(null, 204);
  });

  app.get("/callback", async (c) => {
    const services = c.get("services");
    const q = c.req.query();
    if (q.error) {
      console.info(`sign-in error: ${q.error} ${q.error_description ?? ""}`);
      if (needsAdminApproval(q.error, q.error_description)) {
        return result(c, "admin_approval_required", {
          message: q.error_description || "Your organization requires an administrator to approve Calendar Sync.",
        });
      }
      return error(c, q.error_description || q.error);
    }
    let claims: OAuthState;
    try {
      claims = await readOAuthState(services.settings, q.state ?? "");
    } catch (err) {
      if (err instanceof InvalidToken) return error(c, "Sign-in expired, try again");
      throw err;
    }
    const nonce = getCookie(c, OAUTH_COOKIE);
    if (!nonce || nonce !== claims.nonce || !q.code) return error(c, "Sign-in could not be verified");
    // The nonce is single-use. Set before any redirect below so every response carries it.
    deleteCookie(c, OAUTH_COOKIE, { path: "/auth" });

    const purpose = claims.purpose;
    let reauthSource: ReauthSource | undefined;
    if (purpose === "reauth") {
      if (!isUuid(claims.sid)) return error(c, "Sign-in could not be verified");
      reauthSource = await loadReauthSource(services.sql, claims.sid.toLowerCase());
      if (!reauthSource) return error(c, "This calendar is no longer linked");
    }
    let exchanged: CodeExchange;
    try {
      exchanged = await exchangeCode(services.settings, {
        code: q.code,
        nonce,
        scopes: purpose === "login" ? SIGN_IN_SCOPES : LINK_SCOPES,
        tenant: reauthSource?.tenant_id || "organizations",
      });
    } catch (err) {
      if (err instanceof OidcError) return error(c, err.message);
      throw err;
    }

    if (purpose === "login") return await completeLogin(c, exchanged.identity, claims.next ?? "/calendar");
    if (purpose === "reauth" && reauthSource) return await completeReauth(c, exchanged, claims.uid, reauthSource);
    return await completeLink(c, exchanged, claims.uid);
  });

  return app;
}

async function completeLogin(c: Ctx, identity: SignedInIdentity, nextPath: string): Promise<Response> {
  const { sql, settings } = c.get("services");
  const operator = settings.operatorEmails.has(identity.email);
  const user = await transaction(sql, async (tx) => {
    const [user] = await tx<(UserRow & { inserted: boolean })[]>`
      insert into users (email, display_name, login_tenant_id, login_object_id, is_operator)
      values (${identity.email}, ${identity.name}, ${identity.tenantId}, ${identity.objectId}, ${operator})
      on conflict (login_tenant_id, login_object_id) do update set
        email = excluded.email,
        display_name = excluded.display_name,
        is_operator = users.is_operator or excluded.is_operator
      returning *, (xmax = 0) as inserted
    `;
    await audit(tx, user.inserted ? "user.signed_up" : "user.signed_in", "user", user.id, { actor: user });
    return user;
  });

  setCookie(c, SESSION_COOKIE, await issueSession(settings, user.id), {
    maxAge: settings.sessionTtlHours * 3600,
    httpOnly: true,
    sameSite: "Lax",
    secure: settings.cookiesSecure,
    path: "/",
  });
  return c.redirect(web(c.get("services"), safeNextPath(nextPath)), 302);
}

/** The user who started linking/reconnecting must still be the one signed in. */
async function sessionUser(c: Ctx, stateUid: string | undefined): Promise<UserRow | Response> {
  const { sql, settings } = c.get("services");
  let sessionUid: string;
  try {
    sessionUid = await readSession(settings, getCookie(c, SESSION_COOKIE) ?? "");
  } catch (err) {
    if (err instanceof InvalidToken) return error(c, "Please sign in again");
    throw err;
  }
  if (sessionUid !== stateUid?.toLowerCase()) return error(c, "Session mismatch");
  const [user] = await sql<UserRow[]>`select * from users where id = ${sessionUid}`;
  if (!user) return error(c, "Unknown user");
  return user;
}

function grantProblem(exchanged: CodeExchange): string | null {
  if (!canReadCalendars(exchanged)) {
    return "Calendar access wasn't granted. Try again and accept the permission request.";
  }
  if (!exchanged.refreshToken) return "Microsoft didn't grant offline access to your calendar. Try again.";
  return null;
}

/** Store the fresh delegated grant and (re)activate the source, in the caller's
 * transaction. The access token that came with it is cached, replacing any token minted
 * from the previous grant. */
async function activate(
  services: Services,
  tx: Sql,
  sourceId: string,
  tenantPk: string,
  exchanged: CodeExchange,
  user: UserRow,
  action: string,
): Promise<void> {
  const { cipher } = services;
  await tx`
    update linked_sources set
      source_email = ${exchanged.identity.email},
      refresh_token = ${await cipher.encrypt(exchanged.refreshToken!)},
      granted_scopes = ${exchanged.scope},
      status = ${SourceStatus.ACTIVE},
      reauth_required_at = null,
      last_error = null,
      last_error_at = null,
      access_token = null,
      access_token_expires_at = null
    where id = ${sourceId}
  `;
  if (exchanged.accessToken) {
    await services.tokens.cacheSourceToken(sourceId, exchanged.accessToken, exchanged.expiresIn, tx);
  }
  await audit(tx, action, "linked_source", sourceId, {
    actor: user,
    email: exchanged.identity.email,
    tenant: tenantPk,
  });
}

/** Subscribe (replacing any subscription from the old grant), then sync, after the
 * response. If it can't finish, the sweep recreates the subscription and syncs. */
function startSetup(services: Services, sourceId: string, mode: SyncMode): void {
  services.background(
    runTask(services, "setup_source", () => setupSource(services, sourceId, mode), {
      sourceId,
      onTransient: () => requestResync(services.sql, sourceId, mode === SyncMode.INITIAL),
    }),
  );
}

function titleCase(value: string): string {
  // Python's str.title(): upper-case the first letter of every run of letters.
  return value.toLowerCase().replace(/(^|[^\p{L}])(\p{L})/gu, (_, sep: string, ch: string) => sep + ch.toUpperCase());
}

async function completeLink(c: Ctx, exchanged: CodeExchange, stateUid: string | undefined): Promise<Response> {
  const services = c.get("services");
  const user = await sessionUser(c, stateUid);
  if (user instanceof Response) return user;
  const identity = exchanged.identity;

  const problem = grantProblem(exchanged);
  if (problem) return error(c, problem);

  const outcome = await transaction(services.sql, async (tx) => {
    const domain = identity.email.split("@").pop()!;
    let [tenant] = await tx<TenantRow[]>`select * from tenants where tenant_id = ${identity.tenantId}`;
    if (!tenant) {
      [tenant] = await tx<TenantRow[]>`
        insert into tenants (tenant_id, display_name, domain)
        values (${identity.tenantId}, ${domain}, ${domain})
        returning *
      `;
      await audit(tx, "tenant.discovered", "tenant", tenant.id, { actor: user, tenant_id: identity.tenantId });
    }

    // Proof of ownership: the user just signed in to this exact mailbox (FR-3).
    const [existing] = await tx<{ id: string; status: string }[]>`
      select id, status from linked_sources
      where user_id = ${user.id} and tenant_pk = ${tenant.id} and source_user_id = ${identity.objectId}
      for update
    `;
    if (!existing) {
      const label = tenant.display_name !== domain ? tenant.display_name : titleCase(domain.split(".")[0]);
      const [source] = await tx<{ id: string }[]>`
        insert into linked_sources (user_id, tenant_pk, source_user_id, source_email, label)
        values (${user.id}, ${tenant.id}, ${identity.objectId}, ${identity.email}, ${label})
        returning id
      `;
      await activate(services, tx, source.id, tenant.id, exchanged, user, "source.linked");
      return { status: "linked", sourceId: source.id, mode: SyncMode.INITIAL };
    }
    if (existing.status === SourceStatus.REMOVING) return null;
    const reconnected = existing.status === SourceStatus.REAUTH_REQUIRED;
    await activate(
      services,
      tx,
      existing.id,
      tenant.id,
      exchanged,
      user,
      reconnected ? "source.reconnected" : "source.relinked",
    );
    return { status: reconnected ? "reconnected" : "linked", sourceId: existing.id, mode: SyncMode.RECOVERY };
  });
  if (!outcome) return error(c, "This account is still being unlinked, try again shortly");
  startSetup(services, outcome.sourceId, outcome.mode);
  return result(c, outcome.status, { email: identity.email });
}

async function completeReauth(
  c: Ctx,
  exchanged: CodeExchange,
  stateUid: string | undefined,
  source: ReauthSource,
): Promise<Response> {
  const services = c.get("services");
  const user = await sessionUser(c, stateUid);
  if (user instanceof Response) return user;
  if (source.user_id !== user.id) return error(c, "This calendar is no longer linked");
  if (source.status === SourceStatus.REMOVING) return error(c, "This calendar is being unlinked");
  const identity = exchanged.identity;
  if (identity.tenantId !== source.tenant_id || identity.objectId !== source.source_user_id) {
    console.info(`reauth for source ${source.id} signed in as a different account`);
    return error(
      c,
      `You signed in as ${identity.email}. Sign in as ${source.source_email} to reconnect this calendar.`,
    );
  }
  const problem = grantProblem(exchanged);
  if (problem) return error(c, problem);
  await transaction(
    services.sql,
    (tx) => activate(services, tx, source.id, source.tenant_pk, exchanged, user, "source.reconnected"),
  );
  startSetup(services, source.id, SyncMode.RECOVERY);
  return result(c, "reconnected", { email: identity.email });
}
