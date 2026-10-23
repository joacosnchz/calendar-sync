const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["day", 86_400_000],
  ["hour", 3_600_000],
  ["minute", 60_000],
  ["second", 1000],
];

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const diff = new Date(iso).getTime() - now;
  for (const [unit, ms] of UNITS) {
    if (Math.abs(diff) >= ms || unit === "second") {
      return rtf.format(Math.round(diff / ms), unit);
    }
  }
  return "";
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function timeOfDay(date: Date): string {
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

const LABEL_COLORS = [
  { bg: "bg-sky-100", border: "border-sky-500", text: "text-sky-900", dot: "bg-sky-500" },
  { bg: "bg-violet-100", border: "border-violet-500", text: "text-violet-900", dot: "bg-violet-500" },
  { bg: "bg-emerald-100", border: "border-emerald-500", text: "text-emerald-900", dot: "bg-emerald-500" },
  { bg: "bg-amber-100", border: "border-amber-500", text: "text-amber-900", dot: "bg-amber-500" },
  { bg: "bg-rose-100", border: "border-rose-500", text: "text-rose-900", dot: "bg-rose-500" },
  { bg: "bg-teal-100", border: "border-teal-500", text: "text-teal-900", dot: "bg-teal-500" },
  { bg: "bg-orange-100", border: "border-orange-500", text: "text-orange-900", dot: "bg-orange-500" },
  { bg: "bg-fuchsia-100", border: "border-fuchsia-500", text: "text-fuchsia-900", dot: "bg-fuchsia-500" },
];

export type LabelColor = (typeof LABEL_COLORS)[number];

/** Colours by the user's source order (distinct for up to 8 sources), hashing only unknown labels. */
export function labelColors(labels: string[]): (label: string) => LabelColor {
  const assigned = new Map<string, LabelColor>();
  for (const label of labels) {
    if (!assigned.has(label)) assigned.set(label, LABEL_COLORS[assigned.size % LABEL_COLORS.length]);
  }
  return (label) => assigned.get(label) ?? hashColor(label);
}

function hashColor(label: string): LabelColor {
  let hash = 0x811c9dc5; // FNV-1a
  for (const ch of label) hash = Math.imul(hash ^ ch.charCodeAt(0), 0x01000193) >>> 0;
  return LABEL_COLORS[hash % LABEL_COLORS.length];
}
