"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { CalendarEvent } from "@/lib/api";
import { timeOfDay, type LabelColor } from "@/lib/format";

import { cx } from "./ui";

const HOUR_PX = 48;
const DAY_MS = 86_400_000;

interface Placed {
  event: CalendarEvent;
  start: Date;
  end: Date;
  column: number;
  columns: number;
}

export function startOfWeek(date: Date): Date {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  const offset = (d.getDay() + 6) % 7; // Monday first
  d.setDate(d.getDate() - offset);
  return d;
}

export function addDays(date: Date, days: number): Date {
  const d = new Date(date);
  d.setDate(d.getDate() + days);
  return d;
}

/** Lay out overlapping events side by side, like desktop calendar apps do. */
export function layoutDay(events: CalendarEvent[], dayStart: Date): Placed[] {
  const dayEnd = addDays(dayStart, 1);
  const segments = events
    .filter((e) => !e.is_all_day)
    .map((event) => ({
      event,
      start: new Date(Math.max(new Date(event.start).getTime(), dayStart.getTime())),
      end: new Date(Math.min(new Date(event.end).getTime(), dayEnd.getTime())),
    }))
    .filter((s) => s.end > s.start)
    .sort((a, b) => a.start.getTime() - b.start.getTime() || b.end.getTime() - a.end.getTime());

  const placed: Placed[] = [];
  let cluster: Placed[] = [];
  let clusterEnd = 0;
  const flush = () => {
    const columns = Math.max(1, ...cluster.map((p) => p.column + 1));
    cluster.forEach((p) => (p.columns = columns));
    cluster = [];
  };
  for (const seg of segments) {
    if (cluster.length && seg.start.getTime() >= clusterEnd) flush();
    const taken = new Set(cluster.filter((p) => p.end > seg.start).map((p) => p.column));
    let column = 0;
    while (taken.has(column)) column++;
    const item: Placed = { ...seg, column, columns: 1 };
    cluster.push(item);
    placed.push(item);
    clusterEnd = Math.max(clusterEnd, seg.end.getTime());
  }
  flush();
  return placed;
}

function allDayDates(event: CalendarEvent): [string, string] {
  // All-day events are floating dates; compare as YYYY-MM-DD, never through a time zone.
  return [event.start.slice(0, 10), event.end.slice(0, 10)];
}

function localDate(d: Date): string {
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function WeekView({
  weekStart,
  events,
  colorFor,
}: {
  weekStart: Date;
  events: CalendarEvent[];
  colorFor: (label: string) => LabelColor;
}) {
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)), [weekStart]);
  const scroller = useRef<HTMLDivElement>(null);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    scroller.current?.scrollTo({ top: HOUR_PX * 7 });
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);

  const allDay = events.filter((e) => e.is_all_day);
  const todayKey = localDate(now);

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="grid grid-cols-[64px_repeat(7,1fr)] border-b border-slate-200">
        <div />
        {days.map((d) => {
          const isToday = localDate(d) === todayKey;
          return (
            <div key={d.toISOString()} className="border-l border-slate-100 px-2 py-2 text-center">
              <div className="text-xs uppercase text-slate-500">{d.toLocaleDateString(undefined, { weekday: "short" })}</div>
              <div
                className={cx(
                  "mx-auto mt-0.5 grid h-7 w-7 place-items-center rounded-full text-sm font-medium",
                  isToday ? "bg-brand-500 text-white" : "text-slate-800",
                )}
              >
                {d.getDate()}
              </div>
            </div>
          );
        })}
      </div>

      {allDay.length > 0 && (
        <div className="grid grid-cols-[64px_repeat(7,1fr)] border-b border-slate-200 bg-slate-50/60">
          <div className="px-1 py-1.5 text-right text-[10px] uppercase text-slate-400">all day</div>
          {days.map((d) => {
            const key = localDate(d);
            const today = allDay.filter((e) => {
              const [s, end] = allDayDates(e);
              return s <= key && key < end;
            });
            return (
              <div key={key} className="min-h-8 space-y-1 border-l border-slate-100 p-1">
                {today.map((e) => (
                  <EventChip key={e.key} event={e} color={colorFor(e.labels[0] ?? "")} compact />
                ))}
              </div>
            );
          })}
        </div>
      )}

      <div ref={scroller} className="relative max-h-[70vh] overflow-y-auto">
        <div className="grid grid-cols-[64px_repeat(7,1fr)]" style={{ height: HOUR_PX * 24 }}>
          <div className="relative">
            {Array.from({ length: 24 }, (_, h) => (
              <div key={h} className="absolute right-2 -translate-y-2 whitespace-nowrap text-[11px] text-slate-400" style={{ top: h * HOUR_PX }}>
                {h === 0 ? "" : timeOfDay(new Date(2000, 0, 1, h))}
              </div>
            ))}
          </div>
          {days.map((day) => {
            const placed = layoutDay(events, day);
            const isToday = localDate(day) === todayKey;
            return (
              <div key={day.toISOString()} className="relative border-l border-slate-100">
                {Array.from({ length: 24 }, (_, h) => (
                  <div key={h} className="absolute inset-x-0 border-t border-slate-100" style={{ top: h * HOUR_PX }} />
                ))}
                {placed.map((p) => {
                  const top = ((p.start.getTime() - day.getTime()) / 3_600_000) * HOUR_PX;
                  const height = Math.max(((p.end.getTime() - p.start.getTime()) / 3_600_000) * HOUR_PX, 18);
                  return (
                    <div
                      key={`${p.event.key}-${day.toISOString()}`}
                      className="absolute px-0.5"
                      style={{
                        top,
                        height,
                        left: `${(p.column / p.columns) * 100}%`,
                        width: `${100 / p.columns}%`,
                      }}
                    >
                      <EventChip event={p.event} color={colorFor(p.event.labels[0] ?? "")} start={p.start} end={p.end} />
                    </div>
                  );
                })}
                {isToday && (
                  <div
                    className="pointer-events-none absolute inset-x-0 z-10 border-t-2 border-rose-500"
                    style={{ top: ((now.getTime() - day.getTime()) / DAY_MS) * HOUR_PX * 24 }}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function EventChip({
  event,
  color,
  start,
  end,
  compact = false,
}: {
  event: CalendarEvent;
  color: LabelColor;
  start?: Date;
  end?: Date;
  compact?: boolean;
}) {
  const tentative = event.show_as === "tentative";
  const free = event.show_as === "free";
  return (
    <div
      title={`${event.subject}\n${event.labels.join(" · ")}${event.location ? `\n${event.location}` : ""}\nShow as: ${event.show_as}`}
      className={cx(
        "h-full overflow-hidden rounded-md border-l-4 px-1.5 py-0.5 text-xs leading-tight",
        color.bg,
        color.border,
        color.text,
        tentative && "bg-[repeating-linear-gradient(45deg,transparent,transparent_4px,rgba(255,255,255,.7)_4px,rgba(255,255,255,.7)_8px)]",
        free && "opacity-60",
      )}
    >
      <div className="truncate font-medium">{event.subject || "(no subject)"}</div>
      {!compact && start && end && (
        <div className="truncate opacity-80">
          {timeOfDay(start)} – {timeOfDay(end)}
        </div>
      )}
      {!compact && <div className="truncate opacity-70">{event.labels.join(" · ")}</div>}
    </div>
  );
}
