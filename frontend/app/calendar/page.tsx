"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { addDays, startOfWeek, WeekView } from "@/components/WeekView";
import { Button, Card, ErrorNote, LinkButton, Spinner } from "@/components/ui";
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
  // Sources awaiting sign-in keep their already-synced events, so they stay in the legend.
  const shown = sources.data?.filter((s) => s.status === "active" || s.status === "reauth_required") ?? [];
  const colorFor = labelColors((sources.data ?? []).map((s) => s.label));

  const title = `${weekStart.toLocaleDateString(undefined, { month: "long", day: "numeric" })} – ${addDays(
    weekStart,
    6,
  ).toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" })}`;

  if (sources.data && sources.data.length === 0) return <NoAccounts />;

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
        {shown.map((s) => (
          <span key={s.id} className="flex items-center gap-1.5">
            <span className={`h-2.5 w-2.5 rounded-full ${colorFor(s.label).dot}`} />
            {s.label}
            {s.status === "reauth_required" && <span className="text-xs text-amber-700">(paused)</span>}
          </span>
        ))}
        <span className="ml-auto text-xs text-slate-500">
          Meetings in more than one account are shown once. Striped = tentative, faded = free.
        </span>
      </div>

      <ErrorNote error={events.error} />
      {events.data ? <WeekView weekStart={weekStart} events={events.data} colorFor={colorFor} /> : <Spinner />}
    </div>
  );
}

function NoAccounts() {
  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold text-slate-900">Your calendar</h1>
      <Card className="p-10 text-center">
        <div className="text-lg font-medium text-slate-900">Link an account to fill your calendar</div>
        <p className="mx-auto mt-2 max-w-md text-sm text-slate-600">
          This is where the calendars of all your linked Microsoft 365 accounts come together. Link the account you
          use at each organization and its meetings show up here within seconds.
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-4">
          <LinkButton href="/auth/link">Link your first account</LinkButton>
          <Link href="/dashboard" className="text-sm font-medium text-slate-600 hover:text-slate-900">
            Manage accounts
          </Link>
        </div>
      </Card>
    </div>
  );
}
