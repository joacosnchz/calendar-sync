// Normalising Graph events into the minimal fields we store, and how copies of one meeting
// combine in the unified calendar.

import { WINDOWS_ZONES } from "./windows_zones.ts";

export const STORED_TYPES = new Set(["singleInstance", "occurrence", "exception"]);

// When one meeting is in several sources, the busiest show-as wins (FR-10).
export const SHOW_AS_RANK: Record<string, number> = {
  free: 0,
  unknown: 0,
  workingElsewhere: 1,
  tentative: 2,
  busy: 3,
  oof: 4,
};

const rank = (s: string) => SHOW_AS_RANK[s] ?? 3;

/** The busiest show-as; the first one wins a tie. */
export function busiest(showAs: Iterable<string>): string {
  let best: string | undefined;
  for (const s of showAs) if (best === undefined || rank(s) > rank(best)) best = s;
  if (best === undefined) throw new Error("busiest() of nothing");
  return best;
}

// --- time zones -----------------------------------------------------------------------

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(zone: string): Intl.DateTimeFormat {
  let f = formatters.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    formatters.set(zone, f);
  }
  return f;
}

const resolved = new Map<string, string | null>();

/** Resolve an IANA or Windows time zone name (Graph returns either). */
export function resolveTz(name: string | null | undefined): string | null {
  if (!name) return null;
  if (resolved.has(name)) return resolved.get(name)!;
  let zone: string | null = null;
  for (const candidate of [name, WINDOWS_ZONES[name]]) {
    if (!candidate) continue;
    try {
      formatter(candidate);
      zone = candidate;
      break;
    } catch {
      continue;
    }
  }
  resolved.set(name, zone);
  return zone;
}

/** Offset of `zone` from UTC at the instant `utcMs`, in milliseconds. */
function offsetAt(zone: string, utcMs: number): number {
  const parts: Record<string, number> = {};
  for (const p of formatter(zone).formatToParts(new Date(utcMs))) {
    if (p.type !== "literal") parts[p.type] = Number(p.value);
  }
  const asUtc = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
  return asUtc - (utcMs - (((utcMs % 1000) + 1000) % 1000));
}

/** The UTC instant of a wall-clock time in `zone`, resolved like Python's zoneinfo with
 * fold=0: an ambiguous time (clocks going back) is its first occurrence, and a time that
 * doesn't exist (clocks going forward) uses the offset from before the transition. */
export function zonedToUtc(wallMs: number, zone: string): number {
  const before = offsetAt(zone, wallMs - 86_400_000);
  const after = offsetAt(zone, wallMs + 86_400_000);
  const valid = [...new Set([before, after])]
    .map((offset) => wallMs - offset)
    .filter((utc) => wallMs - offsetAt(zone, utc) === utc);
  if (valid.length) return Math.min(...valid);
  return wallMs - before;
}

const DATETIME_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?$/;

function parseWall(value: string): number {
  const m = DATETIME_RE.exec(value.replace(/Z$/, ""));
  if (!m) throw new Error(`Unparseable Graph dateTime: ${value}`);
  const ms = Number((m[7] ?? "0").slice(0, 3).padEnd(3, "0"));
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] ?? 0), ms);
}

export interface GraphDateTime {
  dateTime: string;
  timeZone?: string | null;
}

/** Parse Graph's dateTimeTimeZone (7 fractional digits) into a UTC instant. */
export function parseGraphDatetime(value: GraphDateTime): Date {
  const wall = parseWall(value.dateTime);
  const zone = resolveTz(value.timeZone || "UTC");
  return new Date(zone ? zonedToUtc(wall, zone) : wall);
}

export function parseIso(value: string | null | undefined): Date | null {
  if (!value) return null;
  // Offsets other than Z are rare from Graph; let Date handle them.
  if (/[+-]\d{2}:\d{2}$/.test(value)) return new Date(value);
  return parseGraphDatetime({ dateTime: value, timeZone: "UTC" });
}

/** All-day events are floating dates: keep the calendar date, ignore the zone. */
export function parseAllDay(value: GraphDateTime): Date {
  const [y, m, d] = value.dateTime.slice(0, 10).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

// --- normalisation --------------------------------------------------------------------

// deno-lint-ignore no-explicit-any
export type RawEvent = Record<string, any>;

export interface NormalizedEvent {
  graphEventId: string;
  icalUid: string | null;
  eventType: string;
  seriesMasterId: string | null;
  subject: string;
  location: string;
  startUtc: Date;
  endUtc: Date;
  startTz: string | null;
  endTz: string | null;
  isAllDay: boolean;
  showAs: string;
  sensitivity: string;
  isCancelled: boolean;
  lastModified: Date | null;
}

export function overlaps(event: NormalizedEvent, start: Date, end: Date): boolean {
  return event.startUtc < end && event.endUtc > start;
}

export function shouldStore(event: NormalizedEvent): boolean {
  return STORED_TYPES.has(event.eventType) && !event.isCancelled;
}

export function normalizeEvent(raw: RawEvent): NormalizedEvent {
  const isAllDay = Boolean(raw.isAllDay);
  const parse = isAllDay ? parseAllDay : parseGraphDatetime;
  return {
    graphEventId: raw.id,
    icalUid: raw.iCalUId || null,
    eventType: raw.type || "singleInstance",
    seriesMasterId: raw.seriesMasterId ?? null,
    subject: raw.subject || "",
    location: raw.location?.displayName || "",
    startUtc: parse(raw.start),
    endUtc: parse(raw.end),
    startTz: raw.originalStartTimeZone || null,
    endTz: raw.originalEndTimeZone || null,
    isAllDay,
    showAs: raw.showAs || "busy",
    sensitivity: raw.sensitivity || "normal",
    isCancelled: Boolean(raw.isCancelled),
    lastModified: parseIso(raw.lastModifiedDateTime),
  };
}

export function dedupKeyFor(sourceId: string, event: NormalizedEvent): string {
  // iCalUId is shared by every copy of a meeting (and is distinct per occurrence), so one
  // meeting that appears in two of a user's sources shows once (FR-14).
  if (event.icalUid) return `ical:${event.icalUid}`;
  return `src:${sourceId}:${event.graphEventId}`;
}
