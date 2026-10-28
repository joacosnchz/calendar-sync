// Delegated access tokens for Microsoft Graph.
//
// Graph is only used to read source calendars (customer tenants, delegated
// Calendars.Read), and there are no app-only tokens. Each linked source stores the refresh
// token its owner granted when they signed in. Access tokens are redeemed from it at the
// source's own tenant endpoint (never `common`), cached (encrypted) on the source row, and
// refreshed under a row lock so concurrent function instances never race on a rotated
// token.

import { jose } from "../deps.ts";
import type { Settings } from "../config.ts";
import { type Sql, transaction } from "../db.ts";
import type { Cipher } from "../fernet.ts";
import { b64urlDecode } from "../fernet.ts";
import { formBody, httpFetch, jsonOrEmpty } from "../http.ts";

// What a source's refresh token is redeemed for. offline_access keeps rotation going.
export const SOURCE_REFRESH_SCOPE = "https://graph.microsoft.com/Calendars.Read offline_access";
const REFRESH_MARGIN_SECONDS = 300;
const REFRESH_LOCK_SECONDS = 35;

// OAuth `error` values on a token response that mean only the user can fix it, by
// signing in again (RFC 6749 invalid_grant, plus the OIDC interaction errors).
export const REAUTH_ERRORS = new Set(["invalid_grant", "interaction_required", "consent_required", "login_required"]);

// AADSTS codes that mean the delegated grant no longer works and the user has to sign in
// again. They are classified as reauth even when `error` is something else.
export const REAUTH_CODES = new Set([
  "AADSTS50076", // MFA required (Conditional Access / per-user MFA)
  "AADSTS50078", // presented MFA claim expired
  "AADSTS50079", // user must register for MFA
  "AADSTS50158", // external security challenge not satisfied
  "AADSTS50173", // grant expired after a password change or reset
  "AADSTS53003", // blocked by Conditional Access
  "AADSTS65001", // user or admin has not consented (consent withdrawn)
  "AADSTS70008", // refresh token / code expired or revoked
  "AADSTS700082", // refresh token expired due to inactivity
  "AADSTS700084", // refresh token (SPA) lifetime exceeded
  "AADSTS530003", // device must be managed (Conditional Access)
  "AADSTS7000229", // service principal missing in the tenant (Enterprise app deleted)
  "AADSTS700016", // application not found in the tenant
  "AADSTS7000112", // application disabled in the tenant
]);

export function isReauthError(error: string | null | undefined, code: string | null | undefined): boolean {
  return REAUTH_ERRORS.has(error ?? "") || REAUTH_CODES.has(code ?? "");
}

export interface TokenErrorInfo {
  code?: string | null;
  error?: string | null;
  transient?: boolean;
  tenantId?: string | null;
  sourceId?: string | null;
  // The source's owner must sign in again (Reconnect); retrying won't help.
  reauthRequired?: boolean;
}

export class TokenError extends Error {
  code: string | null;
  error: string | null;
  transient: boolean;
  tenantId: string | null;
  sourceId: string | null;
  reauthRequired: boolean;

  constructor(message: string, info: TokenErrorInfo = {}) {
    super(message);
    this.code = info.code ?? null;
    this.error = info.error ?? null;
    this.transient = info.transient ?? false;
    this.tenantId = info.tenantId ?? null;
    this.sourceId = info.sourceId ?? null;
    this.reauthRequired = info.reauthRequired ?? false;
  }
}

// --- app credentials ----------------------------------------------------------------------

function pemBlock(pem: string, label: string): string | null {
  const match = new RegExp(`-----BEGIN ${label}-----[\\s\\S]+?-----END ${label}-----`).exec(pem);
  return match ? match[0] : null;
}

function pemBytes(block: string): Uint8Array<ArrayBuffer> {
  return b64urlDecode(block.replace(/-----[A-Z ]+-----/g, "").replace(/\s+/g, ""));
}

const hex = (bytes: Uint8Array) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
const b64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

let signingKey: { pem: string; key: CryptoKey; x5t: string } | undefined;

/** Signed JWT client assertion for certificate credentials. */
async function clientAssertion(settings: Settings, tokenUrl: string): Promise<string> {
  const pem = settings.sourceAppCertPem!;
  if (signingKey?.pem !== pem) {
    const keyBlock = pemBlock(pem, "PRIVATE KEY");
    if (!keyBlock) throw new TokenError("SOURCE_APP_CERT_PEM needs a PKCS#8 'PRIVATE KEY' block");
    let thumbprint = settings.sourceAppCertThumbprint;
    if (!thumbprint) {
      const certBlock = pemBlock(pem, "CERTIFICATE");
      if (!certBlock) {
        throw new TokenError("Set SOURCE_APP_CERT_THUMBPRINT or include the CERTIFICATE in SOURCE_APP_CERT_PEM");
      }
      thumbprint = hex(new Uint8Array(await crypto.subtle.digest("SHA-1", pemBytes(certBlock))));
    }
    const bytes = new Uint8Array(thumbprint.match(/../g)!.map((h) => parseInt(h, 16)));
    signingKey = { pem, key: await jose.importPKCS8(keyBlock, "RS256"), x5t: b64url(bytes) };
  }
  const now = Math.floor(Date.now() / 1000);
  return await new jose.SignJWT({ jti: crypto.randomUUID() })
    .setProtectedHeader({ alg: "RS256", typ: "JWT", x5t: signingKey.x5t })
    .setAudience(tokenUrl)
    .setIssuer(settings.sourceAppClientId)
    .setSubject(settings.sourceAppClientId)
    .setNotBefore(now)
    .setExpirationTime(now + 600)
    .sign(signingKey.key);
}

/** Client authentication for the multi-tenant source app (delegated only). */
export async function clientCredentialsForm(settings: Settings, tokenUrl: string): Promise<Record<string, string>> {
  if (settings.sourceAppCertPem) {
    return {
      client_assertion_type: "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
      client_assertion: await clientAssertion(settings, tokenUrl),
    };
  }
  if (settings.sourceAppClientSecret) return { client_secret: settings.sourceAppClientSecret };
  throw new TokenError(`No credentials configured for app ${settings.sourceAppClientId}`);
}

export function parseAadError(body: Record<string, unknown>): string | null {
  const codes = body.error_codes;
  if (Array.isArray(codes) && codes.length) return `AADSTS${codes[0]}`;
  const match = /AADSTS\d+/.exec(String(body.error_description ?? ""));
  return match ? match[0] : null;
}

export interface RefreshResult {
  accessToken: string;
  expiresIn: number;
  refreshToken: string | null;
  scope: string | null;
}

// --- token service --------------------------------------------------------------------------

export class TokenService {
  constructor(
    private readonly settings: Settings,
    private readonly sql: Sql,
    private readonly cipher: Cipher,
  ) {}

  tokenUrl(tenantId: string): string {
    return `${this.settings.loginBaseUrl}/${tenantId}/oauth2/v2.0/token`;
  }

  // deno-lint-ignore no-explicit-any
  private async post(url: string, data: Record<string, string>, ctx: TokenErrorInfo): Promise<Record<string, any>> {
    let resp: Response;
    try {
      resp = await httpFetch(url, { method: "POST", body: formBody(data) });
    } catch (err) {
      throw new TokenError(`Token endpoint unreachable: ${(err as Error).message}`, { ...ctx, transient: true });
    }
    if (resp.status >= 500) {
      await resp.body?.cancel();
      throw new TokenError(`Token endpoint returned ${resp.status}`, { ...ctx, transient: true });
    }
    const body = await jsonOrEmpty(resp);
    if (resp.status !== 200 || !("access_token" in body)) {
      const code = parseAadError(body);
      const error = body.error ?? null;
      const reauth = ctx.sourceId != null && isReauthError(error, code);
      if (!reauth && resp.status === 429) throw new TokenError("Token endpoint throttled", { ...ctx, transient: true });
      throw new TokenError(body.error_description || `HTTP ${resp.status}`, {
        ...ctx,
        code,
        error,
        reauthRequired: reauth,
      });
    }
    return body;
  }

  // -- per-source delegated tokens ------------------------------------------------------

  /** Cache an access token on the source (inside the caller's transaction if given). */
  async cacheSourceToken(sourceId: string, accessToken: string, expiresIn: number, sql: Sql = this.sql) {
    const ttl = Math.max(Math.floor(expiresIn) - REFRESH_MARGIN_SECONDS, 60);
    await sql`
      update linked_sources
      set access_token = ${await this.cipher.encrypt(accessToken)},
          access_token_expires_at = now() + make_interval(secs => ${ttl})
      where id = ${sourceId}
    `;
  }

  async invalidateSource(sourceId: string): Promise<void> {
    await this.sql`
      update linked_sources set access_token = null, access_token_expires_at = null
      where id = ${sourceId} and access_token is not null
    `;
  }

  private async cached(row: { access_token: string | null; fresh: boolean }): Promise<string | null> {
    return row.access_token && row.fresh ? await this.cipher.decrypt(row.access_token) : null;
  }

  /** A delegated Graph token for one linked source, refreshed when needed.
   *
   * Throws TokenError({reauthRequired: true}) when the user must sign in again. */
  async getSourceToken(sourceId: string): Promise<string> {
    const [row] = await this.sql<{ access_token: string | null; fresh: boolean }[]>`
      select access_token, coalesce(access_token_expires_at > now(), false) as fresh
      from linked_sources where id = ${sourceId}
    `;
    if (!row) throw new TokenError("Linked source no longer exists", { sourceId });
    const cached = await this.cached(row);
    if (cached) return cached;

    try {
      return await transaction(this.sql, async (tx) => {
        await tx.unsafe(`set local lock_timeout = '${REFRESH_LOCK_SECONDS}s'`);
        const [locked] = await tx<
          { refresh_token: string | null; access_token: string | null; fresh: boolean; tenant_id: string | null }[]
        >`
          select ls.refresh_token, ls.access_token, t.tenant_id,
                 coalesce(ls.access_token_expires_at > now(), false) as fresh
          from linked_sources ls join tenants t on t.id = ls.tenant_pk
          where ls.id = ${sourceId}
          for update of ls
        `;
        if (!locked) throw new TokenError("Linked source no longer exists", { sourceId });
        // Another instance may have refreshed while we waited for the lock.
        const fresh = await this.cached(locked);
        if (fresh) return fresh;
        return await this.refreshLocked(tx, sourceId, locked.tenant_id, locked.refresh_token);
      });
    } catch (err) {
      if ((err as { code?: string }).code === "55P03") {
        throw new TokenError(`Timed out waiting for a token refresh: ${(err as Error).message}`, {
          sourceId,
          transient: true,
        });
      }
      throw err;
    }
  }

  private async refreshLocked(
    tx: Sql,
    sourceId: string,
    tenantId: string | null,
    encryptedRefreshToken: string | null,
  ): Promise<string> {
    if (!tenantId) throw new TokenError("Linked source has no tenant id", { sourceId });
    if (!encryptedRefreshToken) {
      throw new TokenError("No delegated access stored for this calendar. Sign in again to reconnect it.", {
        tenantId,
        sourceId,
        reauthRequired: true,
      });
    }
    // The row lock also covers reconnecting: a new refresh token stored by the callback
    // waits for this transaction, so the one we hold is always the newest.
    const refreshToken = await this.cipher.decrypt(encryptedRefreshToken);
    let result: RefreshResult;
    try {
      result = await this.redeemRefreshToken(tenantId, refreshToken, sourceId);
    } catch (err) {
      if (err instanceof TokenError && err.reauthRequired) {
        console.warn(`source ${sourceId} needs reauth (${err.error} ${err.code})`);
      }
      throw err;
    }
    if (result.refreshToken && result.refreshToken !== refreshToken) {
      await tx`
        update linked_sources
        set refresh_token = ${await this.cipher.encrypt(result.refreshToken)},
            granted_scopes = coalesce(${result.scope}, granted_scopes)
        where id = ${sourceId}
      `;
    }
    await this.cacheSourceToken(sourceId, result.accessToken, result.expiresIn, tx);
    return result.accessToken;
  }

  async redeemRefreshToken(tenantId: string, refreshToken: string, sourceId?: string): Promise<RefreshResult> {
    const url = this.tokenUrl(tenantId);
    const body = await this.post(
      url,
      {
        grant_type: "refresh_token",
        client_id: this.settings.sourceAppClientId,
        refresh_token: refreshToken,
        scope: SOURCE_REFRESH_SCOPE,
        ...(await clientCredentialsForm(this.settings, url)),
      },
      { tenantId, sourceId },
    );
    return {
      accessToken: body.access_token,
      expiresIn: Number(body.expires_in ?? 3600),
      refreshToken: body.refresh_token ?? null,
      scope: body.scope ?? null,
    };
  }
}
