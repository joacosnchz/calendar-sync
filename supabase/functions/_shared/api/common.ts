// Request plumbing shared by the routers: errors in FastAPI's `{"detail": ...}` shape
// (what the web app reads), session auth, CSRF and input validation.

import { type Context, getCookie, type Hono } from "../deps.ts";
import type { UserRow } from "../db.ts";
import { CSRF_HEADER, InvalidToken, isUuid, readSession, SESSION_COOKIE } from "../security.ts";
import type { Services } from "../services.ts";

export type AppEnv = { Variables: { services: Services; user: UserRow } };
export type Ctx = Context<AppEnv>;

export class HttpError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(detail);
  }
}

export function installErrorHandler(app: Hono<AppEnv>): void {
  app.onError((err, c) => {
    if (err instanceof HttpError) return c.json({ detail: err.detail }, err.status as 400);
    console.error(`${c.req.method} ${new URL(c.req.url).pathname} failed`, err);
    return c.json({ detail: "Internal Server Error" }, 500);
  });
  app.notFound((c) => c.json({ detail: "Not Found" }, 404));
}

export async function currentUser(c: Ctx): Promise<UserRow> {
  const cached = c.get("user");
  if (cached) return cached;
  const services = c.get("services");
  const token = getCookie(c, SESSION_COOKIE);
  if (!token) throw new HttpError(401, "Not signed in");
  let userId: string;
  try {
    userId = await readSession(services.settings, token);
  } catch (err) {
    if (err instanceof InvalidToken) throw new HttpError(401, "Session expired");
    throw err;
  }
  const [user] = await services.sql<UserRow[]>`select * from users where id = ${userId}`;
  if (!user) throw new HttpError(401, "Unknown user");
  c.set("user", user);
  return user;
}

export async function requireOperator(c: Ctx): Promise<UserRow> {
  const user = await currentUser(c);
  if (!user.is_operator) throw new HttpError(403, "Operator access required");
  return user;
}

/** Session cookies are SameSite=Lax; mutating calls must also carry a custom header,
 * which a cross-site form or simple request cannot set. */
export function csrfProtect(c: Ctx): void {
  if (["POST", "PUT", "PATCH", "DELETE"].includes(c.req.method) && c.req.header(CSRF_HEADER) !== "1") {
    throw new HttpError(403, "Missing CSRF header");
  }
}

export function uuidParam(c: Ctx, name: string): string {
  const value = c.req.param(name);
  if (!isUuid(value)) throw new HttpError(422, `${name} must be a UUID`);
  return value.toLowerCase();
}

export async function jsonBody(c: Ctx): Promise<Record<string, unknown>> {
  try {
    const body = await c.req.json();
    if (body && typeof body === "object" && !Array.isArray(body)) return body;
  } catch {
    // fall through
  }
  throw new HttpError(422, "Request body must be a JSON object");
}

export function optionalString(body: Record<string, unknown>, key: string, min: number, max: number) {
  const value = body[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string" || value.length < min || value.length > max) {
    throw new HttpError(422, `${key} must be a string of ${min} to ${max} characters`);
  }
  return value;
}

export function optionalBool(body: Record<string, unknown>, key: string) {
  const value = body[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "boolean") throw new HttpError(422, `${key} must be a boolean`);
  return value;
}

export function intQuery(c: Ctx, name: string, fallback: number, max: number): number {
  const raw = c.req.query(name);
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value > max) throw new HttpError(422, `${name} must be an integer <= ${max}`);
  return value;
}
