// Unit tests (no I/O): event normalisation, time zones, combining copies of a meeting,
// encryption and session compatibility with the previous Python backend, error
// classification and request guards.

import { assert, assertEquals, assertFalse, assertRejects, assertThrows } from "jsr:@std/assert@1.0.13";
import { resourceId } from "../_shared/api/webhooks.ts";
import {
  busiest,
  dedupKeyFor,
  normalizeEvent,
  parseGraphDatetime,
  type RawEvent,
  resolveTz,
  shouldStore,
  zonedToUtc,
} from "../_shared/events.ts";
import { Cipher, InvalidFernetToken } from "../_shared/fernet.ts";
import { parseRetryAfter, quote } from "../_shared/graph/client.ts";
import { needsAdminApproval } from "../_shared/graph/oidc.ts";
import { isReauthError, parseAadError } from "../_shared/graph/tokens.ts";
import {
  InvalidToken,
  issueOAuthState,
  issueSession,
  readOAuthState,
  readSession,
  safeNextPath,
  timingSafeEqual,
} from "../_shared/security.ts";
import { syncWindow } from "../_shared/timeutil.ts";
import { testSettings } from "./harness.ts";

function rawEvent(overrides: RawEvent = {}): RawEvent {
  return {
    id: "AAMkEvent1",
    iCalUId: "ICAL-1",
    type: "singleInstance",
    subject: "Quarterly review",
    start: { dateTime: "2026-11-02T17:00:00.0000000", timeZone: "UTC" },
    end: { dateTime: "2026-11-02T18:30:00.0000000", timeZone: "UTC" },
    originalStartTimeZone: "Pacific Standard Time",
    originalEndTimeZone: "Pacific Standard Time",
    isAllDay: false,
    showAs: "busy",
    sensitivity: "normal",
    isCancelled: false,
    location: { displayName: "Room 1" },
    lastModifiedDateTime: "2026-10-20T08:00:00.1234567Z",
    attendees: [{ emailAddress: { address: "someone@contoso.example" } }],
    ...overrides,
  };
}

const utc = (...args: [number, number, number, number?, number?, number?, number?]) =>
  new Date(Date.UTC(args[0], args[1] - 1, args[2], args[3] ?? 0, args[4] ?? 0, args[5] ?? 0, args[6] ?? 0));

// --- time zones ---------------------------------------------------------------------------

Deno.test("resolves Windows and IANA time zone names", () => {
  assertEquals(resolveTz("Pacific Standard Time"), "America/Los_Angeles");
  assertEquals(resolveTz("W. Europe Standard Time"), "Europe/Berlin");
  assertEquals(resolveTz("Europe/London"), "Europe/London");
  assertEquals(resolveTz("tzone://Microsoft/Custom"), null);
  assertEquals(resolveTz(null), null);
});

Deno.test("parses seven fractional digits as UTC", () => {
  const dt = parseGraphDatetime({ dateTime: "2026-11-02T17:00:00.1234567", timeZone: "UTC" });
  assertEquals(dt, utc(2026, 11, 2, 17, 0, 0, 123));
});

Deno.test("converts non-UTC input", () => {
  const dt = parseGraphDatetime({ dateTime: "2026-07-01T09:00:00", timeZone: "Pacific Standard Time" });
  assertEquals(dt, utc(2026, 7, 1, 16)); // PDT is UTC-7
  const winter = parseGraphDatetime({ dateTime: "2026-12-01T09:00:00", timeZone: "W. Europe Standard Time" });
  assertEquals(winter, utc(2026, 12, 1, 8)); // CET is UTC+1
});

Deno.test("DST edges resolve like zoneinfo with fold=0", () => {
  // 2026-11-01 01:30 happens twice in New York: the first (EDT, UTC-4) wins.
  assertEquals(new Date(zonedToUtc(Date.UTC(2026, 10, 1, 1, 30), "America/New_York")), utc(2026, 11, 1, 5, 30));
  // 2026-03-08 02:30 doesn't exist in New York: the offset from before the jump (EST).
  assertEquals(new Date(zonedToUtc(Date.UTC(2026, 2, 8, 2, 30), "America/New_York")), utc(2026, 3, 8, 7, 30));
});

Deno.test("sync window starts and ends at midnight UTC", () => {
  const [start, end] = syncWindow(utc(2026, 10, 25, 15, 42), 90, 365);
  assertEquals(start, utc(2026, 7, 27));
  assertEquals(end, utc(2027, 10, 25));
});

// --- normalisation ------------------------------------------------------------------------

Deno.test("keeps only the fields the calendar needs (FR-9)", () => {
  const ev = normalizeEvent(rawEvent({ body: { contentType: "html", content: "<p>Agenda</p>" } }));
  assertEquals(ev.subject, "Quarterly review");
  assertEquals(ev.location, "Room 1");
  assertEquals(ev.startUtc, utc(2026, 11, 2, 17));
  assertEquals(ev.startTz, "Pacific Standard Time");
  assertEquals(ev.lastModified, utc(2026, 10, 20, 8, 0, 0, 123));
  assert(shouldStore(ev));
  assertFalse("attendees" in ev);
  assertFalse("body" in ev);
});

Deno.test("all-day dates are floating", () => {
  const ev = normalizeEvent(rawEvent({
    isAllDay: true,
    start: { dateTime: "2026-11-03T00:00:00.0000000", timeZone: "Pacific Standard Time" },
    end: { dateTime: "2026-11-04T00:00:00.0000000", timeZone: "Pacific Standard Time" },
  }));
  assertEquals(ev.startUtc, utc(2026, 11, 3));
  assertEquals(ev.endUtc, utc(2026, 11, 4));
});

Deno.test("cancelled events and series masters are not stored", () => {
  assertFalse(shouldStore(normalizeEvent(rawEvent({ isCancelled: true }))));
  assertFalse(shouldStore(normalizeEvent(rawEvent({ type: "seriesMaster" }))));
  assert(shouldStore(normalizeEvent(rawEvent({ type: "occurrence" }))));
  assert(shouldStore(normalizeEvent(rawEvent({ type: "exception" }))));
});

Deno.test("dedup key prefers the iCalUId (FR-14)", () => {
  const src = crypto.randomUUID();
  assertEquals(dedupKeyFor(src, normalizeEvent(rawEvent())), "ical:ICAL-1");
  assertEquals(dedupKeyFor(src, normalizeEvent(rawEvent({ iCalUId: null }))), `src:${src}:AAMkEvent1`);
});

Deno.test("the busiest show-as wins (FR-10)", () => {
  assertEquals(busiest(["tentative", "busy"]), "busy");
  assertEquals(busiest(["busy", "oof", "free"]), "oof");
  assertEquals(busiest(["free", "workingElsewhere"]), "workingElsewhere");
  assertEquals(busiest(["free"]), "free");
  // Unknown values count as busy.
  assertEquals(busiest(["tentative", "somethingNew"]), "somethingNew");
  assertEquals(busiest(["oof", "somethingNew"]), "oof");
});

// --- encryption and sessions (compatible with the Python backend) ------------------------

// Made by Python's cryptography.fernet with this key.
const PY_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=";
const PY_TOKEN =
  "gAAAAABq3okl0CZGQPfKH18UvEhP0vpf2QFhYQxpbRaX8p1VrAfBdhL5lWrK50lOOy908eRRm43nppjxYMh2RyT3ETI5W5wa0QBUFHDFmFYGUWqG34hRaNI=";
const PY_EMPTY = "gAAAAABq3oklukSBh_EISyWKyg7jS0JJAYSVPM50zTjahprVFky5jqpu5xx6CnCPxY36be7IGIY5YyBsUgnQswIL47XSNlzKbg==";

Deno.test("decrypts data the Python backend encrypted", async () => {
  const cipher = await Cipher.create([PY_KEY]);
  assertEquals(await cipher.decrypt(PY_TOKEN), "Board prep — Zürich");
  assertEquals(await cipher.decrypt(PY_EMPTY), "");
});

Deno.test("encryption round-trips and supports key rotation", async () => {
  const fresh = "x".repeat(43) + "=";
  const rotated = await Cipher.create([fresh, PY_KEY]);
  const token = await rotated.encrypt("subject");
  assert(token.startsWith("gAAAAA"));
  assertEquals(await rotated.decrypt(token), "subject");
  // Old data still decrypts after prepending a new key...
  assertEquals(await rotated.decrypt(PY_TOKEN), "Board prep — Zürich");
  // ...and the old key alone can't read data written with the new one.
  await assertRejects(async () => await (await Cipher.create([PY_KEY])).decrypt(token), InvalidFernetToken);
  await assertRejects(async () => await rotated.decrypt(token.slice(0, -4) + "AAA="), InvalidFernetToken);
});

Deno.test("accepts sessions the Python backend issued", async () => {
  const settings = testSettings();
  const py =
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXAiOiJzZXNzaW9uIiwic3ViIjoiN2IxYjRmNGUtMmY3Yy00ZDhlLTlhNTEtMGQ2ZjRmMGE5YzExIiwiaWF0IjoxNzkwMDAwMDAwLCJleHAiOjQxMDI0NDQ4MDB9.pCu4lKtnqZCgLUF2thZzulfsDSCe37eSDQRHTcn9aVc";
  assertEquals(await readSession(settings, py), "7b1b4f4e-2f7c-4d8e-9a51-0d6f4f0a9c11");
});

Deno.test("session and OAuth state tokens are not interchangeable", async () => {
  const settings = testSettings();
  const uid = crypto.randomUUID();
  const session = await issueSession(settings, uid);
  assertEquals(await readSession(settings, session), uid);
  const state = await issueOAuthState(settings, { purpose: "login", nonce: "n" });
  assertEquals((await readOAuthState(settings, state)).nonce, "n");
  await assertRejects(() => readSession(settings, state), InvalidToken);
  await assertRejects(() => readOAuthState(settings, session), InvalidToken);
  const reauth = await issueOAuthState(settings, { purpose: "reauth", nonce: "n", userId: uid, sourceId: uid });
  assertEquals((await readOAuthState(settings, reauth)).sid, uid);
  await assertRejects(() => readSession(settings, reauth), InvalidToken);
  const other = testSettings({ SESSION_SECRET: "another-secret-" + "y".repeat(40) });
  await assertRejects(() => readSession(other, session), InvalidToken);
});

Deno.test("only same-site relative redirects are followed", () => {
  assertEquals(safeNextPath("/dashboard"), "/dashboard");
  assertEquals(safeNextPath("//evil.example"), "/calendar");
  assertEquals(safeNextPath("https://evil.example"), "/calendar");
  assertEquals(safeNextPath("/\\evil"), "/calendar");
  assertEquals(safeNextPath(null), "/calendar");
});

Deno.test("constant-time comparison", () => {
  assert(timingSafeEqual("secret", "secret"));
  assertFalse(timingSafeEqual("secret", "secreT"));
  assertFalse(timingSafeEqual("secret", "secret-longer"));
  assertFalse(timingSafeEqual("", "x"));
});

// --- classification -----------------------------------------------------------------------

Deno.test("refresh failures that need the user to sign in again", () => {
  const cases: [string | null, string | null, boolean][] = [
    ["invalid_grant", "AADSTS700082", true],
    ["invalid_grant", null, true],
    ["interaction_required", "AADSTS50076", true],
    ["invalid_client", "AADSTS50079", true],
    ["consent_required", null, true],
    ["invalid_request", "AADSTS53003", true],
    ["unauthorized_client", "AADSTS7000229", true],
    ["invalid_client", "AADSTS7000215", false], // our secret is wrong: operator problem
    ["temporarily_unavailable", null, false],
  ];
  for (const [error, code, expected] of cases) assertEquals(isReauthError(error, code), expected, `${error} ${code}`);
  assertEquals(parseAadError({ error_codes: [700082] }), "AADSTS700082");
  assertEquals(parseAadError({ error_description: "AADSTS50173: The grant expired" }), "AADSTS50173");
  assertEquals(parseAadError({}), null);
});

Deno.test("authorize errors that mean an admin must approve the app", () => {
  const cases: [string, string | null, boolean][] = [
    ["access_denied", "AADSTS90094: The grant requires admin permission.", true],
    ["access_denied", "AADSTS90008: The user or administrator has not consented.", true],
    ["invalid_client", "AADSTS65001: ... Need admin approval", true],
    ["consent_required", null, true],
    ["access_denied", "AADSTS65004: User declined to consent.", false],
    ["access_denied", "AADSTS65001: The user has not consented", false],
  ];
  for (const [error, description, expected] of cases) assertEquals(needsAdminApproval(error, description), expected);
});

Deno.test("Retry-After in seconds or as a date", () => {
  assertEquals(parseRetryAfter("2"), 2);
  assertEquals(parseRetryAfter("0"), 1);
  assertEquals(parseRetryAfter(null), 10);
  assertEquals(parseRetryAfter("soon"), 10);
  const inAMinute = new Date(Date.now() + 60_000).toUTCString();
  assert(Math.abs(parseRetryAfter(inAMinute) - 60) < 2);
});

Deno.test("paths are quoted like urllib.parse.quote", () => {
  assertEquals(quote("alice@contoso.example", "@"), "alice@contoso.example");
  assertEquals(quote("AAMk/x+y=="), "AAMk%2Fx%2By%3D%3D");
  assertEquals(quote("a b(c)!"), "a%20b%28c%29%21");
});

Deno.test("notification resource ids", () => {
  assertEquals(resourceId({ subscriptionId: "s", resourceData: { id: "AAMk1" } }), "AAMk1");
  assertEquals(resourceId({ subscriptionId: "s", resource: "Users/u/Events/AAMk2" }), "AAMk2");
  assertEquals(resourceId({ subscriptionId: "s", resource: "Users('u')/Events('AAMk3')" }), "AAMk3");
  assertEquals(resourceId({ subscriptionId: "s" }), null);
});

Deno.test("settings validation", () => {
  assertThrows(() => testSettings({ SESSION_SECRET: "short" }));
  assertThrows(() => testSettings({ DATA_ENCRYPTION_KEYS: " , " }));
  const s = testSettings({ PUBLIC_WEB_URL: "https://cal.example/" });
  assertEquals(s.oauthRedirectUri, "https://cal.example/auth/callback");
  assert(s.cookiesSecure);
});
