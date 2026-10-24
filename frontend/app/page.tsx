"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { Logo } from "@/components/AppShell";
import { LinkButton } from "@/components/ui";
import type { Me } from "@/lib/api";
import { useResource } from "@/lib/hooks";

const STEPS = [
  {
    title: "Sign in",
    body: "Sign in with any Microsoft work account. There's nothing to install or set up.",
  },
  {
    title: "Link your calendars",
    body: "Sign in with the work account you use at each organization and approve read-only calendar access yourself.",
  },
  {
    title: "See everything in one calendar",
    body: "Meetings from all your linked accounts come together in one calendar here within seconds, with free/busy intact.",
  },
];

export default function Landing() {
  const router = useRouter();
  // Anonymous visitors are expected here, so a 401 must not redirect to sign-in.
  const me = useResource<Me>("/api/me", undefined, { anonymous: true });

  useEffect(() => {
    if (me.data) router.replace("/calendar");
  }, [me.data, router]);

  return (
    <div className="min-h-screen bg-gradient-to-b from-white to-slate-50">
      <header className="mx-auto flex h-16 max-w-5xl items-center px-4">
        <Logo />
      </header>
      <main className="mx-auto max-w-5xl px-4 pb-24 pt-16">
        <div className="max-w-2xl">
          <h1 className="text-4xl font-semibold tracking-tight text-slate-900 sm:text-5xl">
            Every Microsoft 365 calendar you work in, in one place.
          </h1>
          <p className="mt-5 text-lg text-slate-600">
            Consultants, board members and agency teams keep a calendar in every organization they work with.
            Calendar Sync brings the calendars of all your linked Microsoft 365 accounts together in one calendar, so
            you never double-book again.
          </p>
          <div className="mt-8 flex items-center gap-4">
            <LinkButton href="/auth/login?next=/calendar">
              <MicrosoftMark /> Sign in with Microsoft
            </LinkButton>
            <span className="text-sm text-slate-500">Read-only access to your source calendars.</span>
          </div>
        </div>

        <ol className="mt-20 grid gap-6 sm:grid-cols-3">
          {STEPS.map((step, i) => (
            <li key={step.title} className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="text-sm font-semibold text-brand-600">Step {i + 1}</div>
              <div className="mt-1 font-medium text-slate-900">{step.title}</div>
              <p className="mt-2 text-sm text-slate-600">{step.body}</p>
            </li>
          ))}
        </ol>
      </main>
    </div>
  );
}

function MicrosoftMark() {
  return (
    <svg viewBox="0 0 21 21" className="h-4 w-4" aria-hidden>
      <rect x="1" y="1" width="9" height="9" fill="#f25022" />
      <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
      <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
      <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
    </svg>
  );
}
