"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { AppShell } from "@/components/AppShell";
import { Badge, Button, Card, CopyButton, ErrorNote, LinkButton, Spinner, StatusBadge } from "@/components/ui";
import { del, patch, post, type Me, type Source } from "@/lib/api";
import { dateTime, labelColors, relativeTime, type LabelColor } from "@/lib/format";
import { useResource } from "@/lib/hooks";

export default function DashboardPage() {
  return <AppShell>{(me) => <Dashboard me={me} />}</AppShell>;
}

function Dashboard({ me }: { me: Me }) {
  // Poll so linking, backfill and unlinking progress show up without a refresh.
  const sources = useResource<Source[]>("/api/sources", 5000);

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Linked accounts</h1>
          <p className="mt-1 text-sm text-slate-600">
            Calendars you own in other organizations. We only ever read them; changes appear in your unified
            calendar within seconds.
          </p>
        </div>
        <LinkButton href="/auth/link">+ Link an account</LinkButton>
      </div>

      <PrimaryCard me={me} />

      <ErrorNote error={sources.error} />
      {sources.loading && !sources.data ? (
        <Spinner />
      ) : sources.data && sources.data.length === 0 ? (
        <Card className="p-10 text-center">
          <div className="text-lg font-medium text-slate-900">No calendars linked yet</div>
          <p className="mx-auto mt-2 max-w-md text-sm text-slate-600">
            Link the Microsoft 365 account you use at each organization. You&apos;ll sign in to it once to prove
            it&apos;s yours.
          </p>
          <div className="mt-6">
            <LinkButton href="/auth/link">Link your first account</LinkButton>
          </div>
        </Card>
      ) : (
        <div className="space-y-4">
          {sources.data?.map((source) => (
            <SourceCard
              key={source.id}
              source={source}
              color={labelColors(sources.data!.map((s) => s.label))(source.label)}
              onChange={sources.reload}
            />
          ))}
        </div>
      )}

      <DangerZone />
    </div>
  );
}

function PrimaryCard({ me }: { me: Me }) {
  const { status, mailbox, error, mode } = me.primary;
  return (
    <Card className="flex flex-wrap items-center gap-4 p-5">
      <div className="grid h-10 w-10 place-items-center rounded-lg bg-brand-50 text-brand-600">
        <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={2}>
          <rect x="3" y="5" width="18" height="16" rx="2" />
          <path d="M3 10h18" />
        </svg>
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-slate-900">Your unified calendar</span>
          <StatusBadge status={status} />
        </div>
        <div className="mt-0.5 text-sm text-slate-600">
          {status === "ready" && mailbox
            ? mode === "shared_mailbox"
              ? `Calendar "${me.display_name || me.email}" in ${mailbox}`
              : `Primary calendar of ${mailbox}`
            : status === "unassigned"
              ? "An operator is assigning your calendar. Linked accounts will start mirroring once it's ready."
              : status === "provisioning"
                ? "Setting up your calendar…"
                : error}
        </div>
      </div>
    </Card>
  );
}

function SourceCard({
  source,
  color,
  onChange,
}: {
  source: Source;
  color: LabelColor;
  onChange: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(source.label);

  async function run(name: string, action: () => Promise<unknown>) {
    setBusy(name);
    setError(undefined);
    try {
      await action();
      await onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(undefined);
    }
  }

  const sub = source.subscription;
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start gap-4">
        <span className={`mt-1.5 h-3 w-3 shrink-0 rounded-full ${color.dot}`} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            {editing ? (
              <form
                className="flex items-center gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  void run("label", () => patch(`/api/sources/${source.id}`, { label })).then(() =>
                    setEditing(false),
                  );
                }}
              >
                <input
                  autoFocus
                  value={label}
                  maxLength={64}
                  onChange={(e) => setLabel(e.target.value)}
                  className="rounded-md border border-slate-300 px-2 py-1 text-sm"
                />
                <Button size="sm" variant="primary" disabled={!label.trim() || busy === "label"}>
                  Save
                </Button>
                <Button size="sm" type="button" variant="ghost" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </form>
            ) : (
              <>
                <span className="font-medium text-slate-900">{source.label}</span>
                <button
                  className="text-xs text-slate-500 hover:text-slate-800"
                  onClick={() => setEditing(true)}
                  title="Rename the label shown on mirrored events"
                >
                  Rename
                </button>
              </>
            )}
            <StatusBadge status={source.status} />
          </div>
          <div className="mt-0.5 text-sm text-slate-600">
            {source.source_email} · {source.tenant.display_name}
          </div>
        </div>
        <div className="flex gap-2">
          {source.status === "active" && (
            <Button size="sm" disabled={!!busy} onClick={() => run("resync", () => post(`/api/sources/${source.id}/resync`))}>
              {busy === "resync" ? "Queued…" : "Resync"}
            </Button>
          )}
          {source.status !== "removing" && (
            <Button
              size="sm"
              variant="danger"
              disabled={!!busy}
              onClick={() => {
                if (confirm(`Unlink ${source.source_email}? Its events will be removed from your unified calendar.`)) {
                  void run("unlink", () => del(`/api/sources/${source.id}`));
                }
              }}
            >
              Unlink
            </Button>
          )}
        </div>
      </div>

      {source.status === "pending_consent" && source.consent_url && (
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <div className="font-medium">Waiting for {source.tenant.display_name}&apos;s IT admin</div>
          <p className="mt-1">
            An administrator must approve read-only calendar access once for the organization. Send them this link:
          </p>
          <div className="mt-3 flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded bg-white px-2 py-1 text-xs">{source.consent_url}</code>
            <CopyButton value={source.consent_url} label="Copy link" />
          </div>
        </div>
      )}
      {source.status === "revoked" && (
        <div className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
          {source.tenant.display_name}&apos;s administrator removed access. Mirrored events from this account were
          removed.
        </div>
      )}

      {source.status === "active" && (
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 border-t border-slate-100 pt-4 text-sm sm:grid-cols-4">
          <Metric label="Events mirrored" value={source.event_count.toString()} />
          <Metric
            label="Last change received"
            value={relativeTime(source.last_notification_at)}
            title={dateTime(source.last_notification_at)}
          />
          <Metric label="Last full check" value={relativeTime(source.last_synced_at)} title={dateTime(source.last_synced_at)} />
          <Metric
            label="Live updates"
            value={
              sub ? (
                <span className="flex items-center gap-1.5">
                  <Badge tone={sub.status === "active" ? "good" : "bad"}>{sub.status === "active" ? "on" : "failing"}</Badge>
                  <span className="text-xs text-slate-500">renews {relativeTime(sub.renew_due_at)}</span>
                </span>
              ) : (
                <Badge tone="warn">setting up</Badge>
              )
            }
          />
        </dl>
      )}
      {source.last_error && (
        <div className="mt-3">
          <ErrorNote error={`Last error ${relativeTime(source.last_error_at)}: ${source.last_error}`} />
        </div>
      )}
      {error && (
        <div className="mt-3">
          <ErrorNote error={error} />
        </div>
      )}
    </Card>
  );
}

function Metric({ label, value, title }: { label: string; value: React.ReactNode; title?: string }) {
  return (
    <div title={title}>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-0.5 font-medium text-slate-800">{value}</dd>
    </div>
  );
}

function DangerZone() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  return (
    <Card className="flex flex-wrap items-center justify-between gap-4 border-rose-100 p-5">
      <div>
        <div className="font-medium text-slate-900">Delete account</div>
        <p className="text-sm text-slate-600">
          Removes every mirrored event, your unified calendar and all data we store about you.
        </p>
      </div>
      <Button
        variant="danger"
        disabled={busy}
        onClick={async () => {
          if (!confirm("Delete your account and remove all mirrored events? This can't be undone.")) return;
          setBusy(true);
          await del("/api/me");
          router.replace("/");
        }}
      >
        Delete account
      </Button>
    </Card>
  );
}
