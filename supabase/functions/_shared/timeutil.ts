export const SECOND = 1000;
export const MINUTE = 60 * SECOND;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;

export function utcnow(): Date {
  return new Date();
}

export function addMs(date: Date, ms: number): Date {
  return new Date(date.getTime() + ms);
}

/** Format for Graph query parameters and subscription expiry (UTC, milliseconds, 'Z'). */
export function isoformatZ(date: Date): string {
  return date.toISOString();
}

function midnightUtc(ms: number): Date {
  const d = new Date(ms);
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

export function syncWindow(now: Date, pastDays: number, futureDays: number): [Date, Date] {
  return [midnightUtc(now.getTime() - pastDays * DAY), midnightUtc(now.getTime() + futureDays * DAY)];
}

export const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
