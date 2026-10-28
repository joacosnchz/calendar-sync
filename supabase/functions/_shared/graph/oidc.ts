// Delegated OpenID Connect sign-in.
//
// Platform sign-in only proves who the user is (openid profile email User.Read). Linking a
// source calendar (and reconnecting one) asks the mailbox owner for delegated
// Calendars.Read plus offline_access (incremental consent), which every user can grant for
// themselves: no tenant admin is involved. The resulting refresh token is stored per source
// (see graph/tokens.ts).

import { jose } from "../deps.ts";
import type { Settings } from "../config.ts";
import { formBody, httpFetch, jsonOrEmpty } from "../http.ts";
import { clientCredentialsForm } from "./tokens.ts";

export const SIGN_IN_SCOPES = "openid profile email User.Read";
export const LINK_SCOPES = "openid profile email offline_access User.Read Calendars.Read";
const REQUIRED_SOURCE_SCOPE = "calendars.read";
// Allowed clock difference when checking the ID token's expiry.
const CLOCK_SKEW_SECONDS = 300;

// Authorize-endpoint errors meaning the tenant doesn't let users consent for themselves
// (an admin has to approve the app, or user consent is disabled).
const ADMIN_APPROVAL_CODES = ["AADSTS90094", "AADSTS90008"];

export class OidcError extends Error {}

export interface SignedInIdentity {
  tenantId: string;
  objectId: string;
  email: string;
  name: string;
}

export interface CodeExchange {
  identity: SignedInIdentity;
  accessToken: string | null;
  expiresIn: number;
  refreshToken: string | null;
  scope: string;
}

export function canReadCalendars(result: CodeExchange): boolean {
  const granted = new Set(result.scope.split(/\s+/).filter(Boolean).map((s) => s.split("/").pop()!.toLowerCase()));
  return granted.has(REQUIRED_SOURCE_SCOPE);
}

export function needsAdminApproval(error: string | null | undefined, description: string | null | undefined): boolean {
  const text = description ?? "";
  if (ADMIN_APPROVAL_CODES.some((code) => text.includes(code))) return true;
  if (text.includes("AADSTS65001") && text.toLowerCase().includes("admin")) return true;
  return error === "consent_required";
}

export function authorizeUrl(
  settings: Settings,
  opts: {
    state: string;
    nonce: string;
    scopes?: string;
    tenant?: string;
    loginHint?: string | null;
    prompt?: string | null;
  },
): string {
  // Work and school accounts only; the token's `tid` tells us which tenant.
  const params = new URLSearchParams({
    client_id: settings.sourceAppClientId,
    response_type: "code",
    redirect_uri: settings.oauthRedirectUri,
    response_mode: "query",
    scope: opts.scopes ?? SIGN_IN_SCOPES,
    state: opts.state,
    nonce: opts.nonce,
  });
  const prompt = opts.prompt === undefined ? "select_account" : opts.prompt;
  if (prompt) params.set("prompt", prompt);
  if (opts.loginHint) params.set("login_hint", opts.loginHint);
  return `${settings.browserLoginBaseUrl}/${opts.tenant ?? "organizations"}/oauth2/v2.0/authorize?${params}`;
}

export async function exchangeCode(
  settings: Settings,
  opts: { code: string; nonce: string; scopes?: string; tenant?: string },
): Promise<CodeExchange> {
  const tokenUrl = `${settings.loginBaseUrl}/${opts.tenant ?? "organizations"}/oauth2/v2.0/token`;
  let resp: Response;
  try {
    resp = await httpFetch(tokenUrl, {
      method: "POST",
      body: formBody({
        grant_type: "authorization_code",
        client_id: settings.sourceAppClientId,
        code: opts.code,
        redirect_uri: settings.oauthRedirectUri,
        scope: opts.scopes ?? SIGN_IN_SCOPES,
        ...(await clientCredentialsForm(settings, tokenUrl)),
      }),
    });
  } catch {
    throw new OidcError("Microsoft sign-in is unreachable, try again");
  }
  const body = await jsonOrEmpty(resp);
  if (resp.status !== 200 || !("id_token" in body)) {
    throw new OidcError(body.error_description || `Token exchange failed (${resp.status})`);
  }
  return {
    identity: validateIdToken(body.id_token, settings, opts.nonce),
    accessToken: body.access_token ?? null,
    expiresIn: Number(body.expires_in ?? 3600),
    refreshToken: body.refresh_token ?? null,
    scope: body.scope ?? "",
  };
}

export function validateIdToken(idToken: string, settings: Settings, nonce: string): SignedInIdentity {
  // The ID token came straight from the token endpoint over TLS (authorization code flow),
  // so per OIDC Core 3.1.3.7 TLS authenticates the issuer and the signature check may be
  // skipped. Every claim is still validated.
  let claims: jose.JWTPayload & Record<string, unknown>;
  try {
    claims = jose.decodeJwt(idToken);
  } catch (err) {
    throw new OidcError(`Invalid ID token: ${(err as Error).message}`);
  }
  for (const claim of ["exp", "aud", "iss", "tid", "oid"]) {
    if (claims[claim] === undefined) throw new OidcError(`Invalid ID token: Token is missing the "${claim}" claim`);
  }
  if (typeof claims.exp !== "number" || claims.exp + CLOCK_SKEW_SECONDS < Date.now() / 1000) {
    throw new OidcError("Invalid ID token: Signature has expired");
  }
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!audiences.includes(settings.sourceAppClientId)) throw new OidcError("Invalid ID token: Audience doesn't match");
  const tid = String(claims.tid);
  const oid = String(claims.oid);
  if (claims.iss !== `${settings.loginBaseUrl}/${tid}/v2.0`) throw new OidcError("ID token issuer mismatch");
  if (claims.nonce !== nonce) throw new OidcError("ID token nonce mismatch");
  const email = String(claims.email || claims.preferred_username || "").toLowerCase();
  if (!email) throw new OidcError("ID token has no email or preferred_username");
  return { tenantId: tid, objectId: oid, email, name: String(claims.name || email) };
}
