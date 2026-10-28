// Session cookies and signed OAuth state (HS256 JWTs, compatible with the sessions the
// previous backend issued).

import { jose } from "./deps.ts";
import type { Settings } from "./config.ts";

export const SESSION_COOKIE = "cs_session";
export const OAUTH_COOKIE = "cs_oauth";
export const CSRF_HEADER = "x-calsync-csrf";

export class InvalidToken extends Error {}

export type Claims = Record<string, unknown>;

const keys = new Map<string, Uint8Array>();

function key(settings: Settings): Uint8Array {
  let k = keys.get(settings.sessionSecret);
  if (!k) {
    k = new TextEncoder().encode(settings.sessionSecret);
    keys.set(settings.sessionSecret, k);
  }
  return k;
}

async function encode(settings: Settings, claims: Claims, ttlSeconds: number): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  return await new jose.SignJWT(claims)
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setIssuedAt(now)
    .setExpirationTime(now + ttlSeconds)
    .sign(key(settings));
}

async function decode(settings: Settings, token: string, typ: string): Promise<Claims> {
  let claims: Claims;
  try {
    ({ payload: claims } = await jose.jwtVerify(token, key(settings), { algorithms: ["HS256"] }));
  } catch (err) {
    throw new InvalidToken(err instanceof Error ? err.message : String(err));
  }
  if (claims.typ !== typ) throw new InvalidToken("Wrong token type");
  return claims;
}

export function issueSession(settings: Settings, userId: string): Promise<string> {
  return encode(settings, { typ: "session", sub: userId }, settings.sessionTtlHours * 3600);
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_RE.test(value);
}

export async function readSession(settings: Settings, token: string): Promise<string> {
  const sub = (await decode(settings, token, "session")).sub;
  if (!isUuid(sub)) throw new InvalidToken("Bad subject");
  return sub.toLowerCase();
}

export function newNonce(): string {
  // Same shape as Python's secrets.token_urlsafe(24).
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export interface OAuthState {
  purpose: "login" | "link" | "reauth";
  nonce: string;
  next: string;
  uid?: string;
  sid?: string;
}

/** purpose: "login" (platform sign-in), "link" (add a source) or "reauth" (reconnect the
 * source `sourceId`). */
export function issueOAuthState(
  settings: Settings,
  opts: { purpose: OAuthState["purpose"]; nonce: string; userId?: string; nextPath?: string; sourceId?: string },
): Promise<string> {
  const claims: Claims = { typ: "oauth", purpose: opts.purpose, nonce: opts.nonce, next: opts.nextPath ?? "/" };
  if (opts.userId) claims.uid = opts.userId;
  if (opts.sourceId) claims.sid = opts.sourceId;
  return encode(settings, claims, 15 * 60);
}

export async function readOAuthState(settings: Settings, token: string): Promise<OAuthState> {
  const claims = await decode(settings, token, "oauth");
  return claims as unknown as OAuthState;
}

/** Only allow same-site relative redirects. */
export function safeNextPath(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return "/calendar";
  return value;
}

/** Constant-time string comparison. */
export function timingSafeEqual(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}
