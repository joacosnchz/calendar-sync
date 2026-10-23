"use client";

import { useMemo, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { addDays, startOfWeek, WeekView } from "@/components/WeekView";
import { Button, ErrorNote, Spinner } from "@/components/ui";
import type { CalendarEvent, Source } from "@/lib/api";
import { labelColors } from "@/lib/format";
import { useResource } from "@/lib/hooks";

export default function CalendarPage() {
  return <AppShell>{() => <Calendar />}</AppShell>;
}

function Calendar() {
  const [weekStart, setWeekStart] = useState(() => startOfWeek(new Date()));
  const weekEnd = useMemo(() => addDays(weekStart, 7), [weekStart]);
  const query = new URLSearchParams({ start: weekStart.toISOString(), end: weekEnd.toISOString() });
  const events = useResource<CalendarEvent[]>(`/api/calendar?${query}`, 10_000);
  const sources = useResource<Source[]>("/api/sources");
  const active = sources.data?.filter((s) => s.status === "active") ?? [];
  const colorFor = labelColors((sources.data ?? []).map((s) => s.label));

  const title = `${weekStart.toLocaleDateString(undefined, { month: "long", day: "numeric" })} – ${addDays(
    weekStart,
    6,
  ).toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" })}`;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="mr-auto text-2xl font-semibold text-slate-900">{title}</h1>
        <Button onClick={() => setWeekStart(startOfWeek(new Date()))}>Today</Button>
        <Button aria-label="Previous week" onClick={() => setWeekStart(addDays(weekStart, -7))}>
          ‹
        </Button>
        <Button aria-label="Next week" onClick={() => setWeekStart(addDays(weekStart, 7))}>
          ›
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-4 text-sm text-slate-600">
        {active.map((s) => (
          <span key={s.id} className="flex items-center gap-1.5">
            <span className={`h-2.5 w-2.5 rounded-full ${colorFor(s.label).dot}`} />
            {s.label}
          </span>
        ))}
        <span className="ml-auto text-xs text-slate-500">
          This is the same view your unified Outlook calendar shows. Striped = tentative, faded = free.
        </span>
      </div>

      <ErrorNote error={events.error} />
      {events.data ? <WeekView weekStart={weekStart} events={events.data} colorFor={colorFor} /> : <Spinner />}
    </div>
  );
}
