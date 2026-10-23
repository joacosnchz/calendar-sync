import Link from "next/link";

import { Logo } from "./AppShell";
import { cx } from "./ui";

export function ResultPage({
  tone,
  title,
  children,
  action,
}: {
  tone: "good" | "warn" | "bad";
  title: string;
  children?: React.ReactNode;
  action?: { href: string; label: string };
}) {
  return (
    <div className="min-h-screen">
      <header className="mx-auto flex h-16 max-w-5xl items-center px-4">
        <Logo />
      </header>
      <main className="mx-auto max-w-lg px-4 pt-16">
        <div className="rounded-xl border border-slate-200 bg-white p-8 shadow-sm">
          <div
            className={cx(
              "grid h-10 w-10 place-items-center rounded-full text-lg",
              tone === "good" && "bg-emerald-50 text-emerald-600",
              tone === "warn" && "bg-amber-50 text-amber-600",
              tone === "bad" && "bg-rose-50 text-rose-600",
            )}
          >
            {tone === "good" ? "✓" : tone === "warn" ? "!" : "×"}
          </div>
          <h1 className="mt-4 text-xl font-semibold text-slate-900">{title}</h1>
          <div className="mt-2 space-y-3 text-sm text-slate-600">{children}</div>
          {action && (
            <Link
              href={action.href}
              className="mt-6 inline-flex rounded-lg bg-brand-500 px-3.5 py-2 text-sm font-medium text-white hover:bg-brand-600"
            >
              {action.label}
            </Link>
          )}
        </div>
      </main>
    </div>
  );
}
