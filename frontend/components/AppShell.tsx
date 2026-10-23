"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { post, type Me } from "@/lib/api";
import { useMe } from "@/lib/hooks";

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
  const router = useRouter();
  const me = useMe({ requireOperator });

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
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="hidden text-slate-600 sm:inline">{me.data.email}</span>
            <button
              className="rounded-md px-2 py-1 text-slate-600 hover:bg-slate-100"
              onClick={async () => {
                await post("/auth/logout");
                router.replace("/");
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
