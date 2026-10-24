"use client";

import { useState } from "react";

import { AppShell } from "@/components/AppShell";
import { Badge, Button, Card, ErrorNote, LinkButton, Spinner, StatusBadge } from "@/components/ui";
import { del, navigate, patch, post, type Me, type Source } from "@/lib/api";
import { dateTime, labelColors, reauthSummary, relativeTime, type LabelColor } from "@/lib/format";
import { useResource } from "@/lib/hooks";

export default function DashboardPage() {
  return <AppShell>{(me) => <Dashboard me={me} />}</AppShell>;
}

function Dashboard({ me }: { me: Me }) {
  // Poll so linking, backfill and unlinking progress show up without a refresh.
  const sources = useResource<Source[]>("/api/sources", 5000);
  const needReauth = sources.data?.filter((s) => s.status === "reauth_required") ?? [];

  return (
    <div className="space-y-8">
      {needReauth.length > 0 && <ReauthBanner sources={needReauth} />}

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

function ReauthBanner({ sources }: { sources: Source[] }) {
  return (
    <div className="flex flex-wrap items-center gap-4 rounded-xl border border-amber-300 bg-amber-50 p-5 text-amber-900">
      <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-amber-100 text-lg font-semibold text-amber-700">
        !
      </div>
      <div className="min-w-0 flex-1">
        <div className="font-medium">{reauthSummary(sources.length)}</div>
        <p className="mt-0.5 text-sm">
          Syncing is paused for {sources.map((s) => s.source_email).join(", ")}. Events already in your unified
          calendar stay there, but new changes won&apos;t appear until you reconnect
          {sources.length > 1 ? " each account below." : "."}
        </p>
      </div>
      {sources.length === 1 && <LinkButton href={reauthHref(sources[0])}>Reconnect</LinkButton>}
    </div>
  );
}

// The backend always sets reauth_url for reauth_required sources; fall back to the route it points at.
function reauthHref(source: Source) {
  return source.reauth_url ?? `/auth/reauth/${source.id}`;
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

      {source.status === "reauth_required" && (
        <div className="mt-4 flex flex-wrap items-start gap-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <div className="min-w-0 flex-1">
            <div className="font-medium">Sign in to {source.source_email} again</div>
            <p className="mt-1">
              Syncing paused{" "}
              {source.reauth_required_at && (
                <span title={dateTime(source.reauth_required_at)}>{relativeTime(source.reauth_required_at)} </span>
              )}
              because our access to this calendar expired or was revoked. Events already mirrored from it stay in your
              unified calendar; once you reconnect, changes made in the meantime are caught up automatically.
            </p>
            {source.last_error && <p className="mt-2 text-xs text-amber-800">Reason: {source.last_error}</p>}
          </div>
          <LinkButton href={reauthHref(source)}>Reconnect</LinkButton>
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
      {source.last_error && source.status !== "reauth_required" && (
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
          navigate("/");
        }}
      >
        Delete account
      </Button>
    </Card>
  );
}
