"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { navigate, post, type Me, type Source } from "@/lib/api";
import { reauthSummary } from "@/lib/format";
import { useMe, useResource } from "@/lib/hooks";

import { cx, Spinner } from "./ui";

export function Logo() {
  return (
    <span className="flex items-center gap-2 font-semibold text-slate-900">
      <span className="grid h-7 w-7 place-items-center rounded-lg bg-brand-500 text-white">
        <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth={2}>
          <rect x="3" y="5" width="18" height="16" rx="2" />
          <path d="M3 10h18M8 3v4M16 3v4" />
        </svg>
      </span>
      Calendar Sync
    </span>
  );
}

const NAV = [
  { href: "/dashboard", label: "Accounts" },
  { href: "/calendar", label: "Calendar" },
];

export function AppShell({
  children,
  requireOperator = false,
}: {
  children: (me: Me) => React.ReactNode;
  requireOperator?: boolean;
}) {
  const pathname = usePathname();
  const me = useMe({ requireOperator });
  // Only for the "needs sign-in" dot on Accounts; the dashboard polls sources itself.
  const sources = useResource<Source[]>(me.data ? "/api/sources" : null, 60_000);
  const reauthCount = sources.data?.filter((s) => s.status === "reauth_required").length ?? 0;

  if (!me.data || (requireOperator && !me.data.is_operator)) {
    return (
      <div className="grid min-h-screen place-items-center">
        <Spinner />
      </div>
    );
  }

  const nav = me.data.is_operator ? [...NAV, { href: "/admin", label: "Operations" }] : NAV;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-4">
          <Link href="/dashboard">
            <Logo />
          </Link>
          <nav className="flex items-center gap-1">
            {nav.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={cx(
                  "rounded-md px-3 py-1.5 text-sm font-medium",
                  pathname.startsWith(item.href)
                    ? "bg-slate-100 text-slate-900"
                    : "text-slate-600 hover:bg-slate-50 hover:text-slate-900",
                )}
              >
                {item.label}
                {item.href === "/dashboard" && reauthCount > 0 && (
                  <span
                    className="ml-1.5 inline-block h-2 w-2 rounded-full bg-amber-500 align-middle"
                    title={reauthSummary(reauthCount)}
                  >
                    <span className="sr-only">(sign-in needed)</span>
                  </span>
                )}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="hidden text-slate-600 sm:inline">{me.data.email}</span>
            <button
              className="rounded-md px-2 py-1 text-slate-600 hover:bg-slate-100"
              onClick={async () => {
                await post("/auth/logout");
                navigate("/");
              }}
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8">{children(me.data)}</main>
    </div>
  );
}
