"use client";

import { useState } from "react";

import { AppShell } from "@/components/AppShell";
import { Badge, Button, Card, cx, ErrorNote, Spinner, Stat, StatusBadge } from "@/components/ui";
import {
  patch,
  post,
  type AdminSubscription,
  type AdminUser,
  type AuditRow,
  type Me,
  type Overview,
  type SyncErrorRow,
  type Tenant,
} from "@/lib/api";
import { dateTime, relativeTime } from "@/lib/format";
import { useResource } from "@/lib/hooks";

const TABS = ["Tenants", "Users", "Subscriptions", "Errors", "Audit log"] as const;
type Tab = (typeof TABS)[number];

export default function AdminPage() {
  return <AppShell requireOperator>{(me) => <Admin me={me} />}</AppShell>;
}

function Admin({ me }: { me: Me }) {
  const [tab, setTab] = useState<Tab>("Tenants");
  const overview = useResource<Overview>("/api/admin/overview", 10_000);
  const o = overview.data;
  const sum = (r?: Record<string, number>) => Object.values(r ?? {}).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Operations</h1>
        <p className="mt-1 text-sm text-slate-600">Provision primary calendars, set tenant privacy and watch sync health.</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Tenants" value={o?.tenants ?? "–"} hint={`${o?.users ?? "–"} users`} />
        <Stat
          label="Active sources"
          value={o ? (o.sources.active ?? 0) : "–"}
          hint={o ? `${o.sources.reauth_required ?? 0} need sign-in · ${sum(o.sources)} total` : undefined}
        />
        <Stat
          label="Subscriptions"
          value={o?.subscriptions ?? "–"}
          hint={o ? `${o.subscriptions_failing} failing · ${o.subscriptions_expiring_24h} expire < 24h` : undefined}
        />
        <Stat label="Errors (24h)" value={o?.errors_24h ?? "–"} hint={`${o?.mirrors ?? "–"} mirrored events`} />
      </div>

      <div className="flex gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={cx(
              "-mb-px border-b-2 px-3 py-2 text-sm font-medium",
              tab === t ? "border-brand-500 text-brand-700" : "border-transparent text-slate-600 hover:text-slate-900",
            )}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "Tenants" && <Tenants onChange={overview.reload} />}
      {tab === "Users" && <Users me={me} />}
      {tab === "Subscriptions" && <Subscriptions />}
      {tab === "Errors" && <Errors />}
      {tab === "Audit log" && <AuditLog />}
    </div>
  );
}

function Table({ head, children }: { head: string[]; children: React.ReactNode }) {
  return (
    <Card className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            {head.map((h) => (
              <th key={h} className="px-4 py-2.5 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
    </Card>
  );
}

function Td({ children, className, title }: { children?: React.ReactNode; className?: string; title?: string }) {
  return (
    <td title={title} className={cx("px-4 py-3 align-top", className)}>
      {children}
    </td>
  );
}

// --- tenants ---------------------------------------------------------------------------

function Tenants({ onChange }: { onChange: () => Promise<void> }) {
  const tenants = useResource<Tenant[]>("/api/admin/tenants", 10_000);
  const [error, setError] = useState<string>();

  async function act(action: () => Promise<unknown>) {
    setError(undefined);
    try {
      await action();
      await Promise.all([tenants.reload(), onChange()]);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600">
        Tenants appear here the first time someone links a calendar from them. Each user approves read-only{" "}
        <b>Calendars.Read</b> (delegated) access for themselves; organizations that block user consent must approve
        Calendar Sync in their own tenant.
      </p>
      <ErrorNote error={error ?? tenants.error} />
      {!tenants.data ? (
        <Spinner />
      ) : tenants.data.length === 0 ? (
        <Card className="p-8 text-center text-sm text-slate-500">No tenants yet. They appear once a user links a calendar.</Card>
      ) : (
        <Table head={["Tenant", "Sources", "Last notification", "Next expiry", "Errors 24h", ""]}>
          {tenants.data.map((t) => (
            <tr key={t.id}>
              <Td>
                <div className="font-medium text-slate-900">{t.display_name}</div>
                <div className="font-mono text-xs text-slate-500">{t.tenant_id ?? t.domain ?? "—"}</div>
                {!t.copy_details && (
                  <div className="mt-1">
                    <Badge tone="info">privacy: busy only</Badge>
                  </div>
                )}
                {t.last_error && <div className="mt-1 max-w-56 text-xs text-rose-700">{t.last_error}</div>}
              </Td>
              <Td>
                <div className="tabular-nums">
                  {t.active_source_count} / {t.source_count} active
                </div>
                {t.reauth_required_count > 0 && (
                  <div className="mt-1">
                    <Badge tone="warn">{t.reauth_required_count} sign-in needed</Badge>
                  </div>
                )}
              </Td>
              <Td title={dateTime(t.last_notification_at)}>{relativeTime(t.last_notification_at)}</Td>
              <Td title={dateTime(t.next_subscription_expiry)}>
                {t.next_subscription_expiry ? relativeTime(t.next_subscription_expiry) : "—"}
              </Td>
              <Td>{t.errors_24h ? <Badge tone="bad">{t.errors_24h}</Badge> : <span className="text-slate-400">0</span>}</Td>
              <Td className="whitespace-nowrap text-right">
                <Button
                  size="sm"
                  onClick={() => act(() => patch(`/api/admin/tenants/${t.id}`, { copy_details: !t.copy_details }))}
                  title="Whether mirrors may show meeting subjects and locations from this tenant"
                >
                  {t.copy_details ? "Hide details" : "Show details"}
                </Button>
              </Td>
            </tr>
          ))}
        </Table>
      )}
    </div>
  );
}

// --- users -----------------------------------------------------------------------------

function Users({ me }: { me: Me }) {
  const users = useResource<AdminUser[]>("/api/admin/users", 10_000);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const assignable = me.primary.mode === "user_mailbox";

  async function act(action: () => Promise<unknown>, message?: string) {
    setError(undefined);
    setNotice(undefined);
    try {
      await action();
      if (message) setNotice(message);
      await users.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600">
        Primary calendar hosting: <b>{me.primary.mode === "shared_mailbox" ? "one calendar per user in a shared mailbox" : "a mailbox per user, assigned here"}</b>.
      </p>
      <ErrorNote error={error ?? users.error} />
      {notice && <div className="rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{notice}</div>}
      {!users.data ? (
        <Spinner />
      ) : (
        <Table head={["User", "Primary calendar", "Sources", "Mirrors", "Joined", ""]}>
          {users.data.map((u) => (
            <tr key={u.id}>
              <Td>
                <div className="font-medium text-slate-900">{u.display_name || u.email}</div>
                <div className="text-xs text-slate-500">{u.email}</div>
                {u.is_operator && (
                  <div className="mt-1">
                    <Badge tone="info">operator</Badge>
                  </div>
                )}
              </Td>
              <Td>
                <StatusBadge status={u.primary_status} />
                <div className="mt-1 text-xs text-slate-500">{u.primary_mailbox ?? "—"}</div>
                {u.primary_error && <div className="mt-1 max-w-64 text-xs text-rose-700">{u.primary_error}</div>}
                {assignable && <AssignMailbox user={u} onAssign={(m) => act(() => patch(`/api/admin/users/${u.id}`, { primary_mailbox: m }))} />}
              </Td>
              <Td className="tabular-nums">{u.source_count}</Td>
              <Td className="tabular-nums">{u.mirror_count}</Td>
              <Td>{relativeTime(u.created_at)}</Td>
              <Td className="space-x-1.5 whitespace-nowrap text-right">
                <Button size="sm" onClick={() => act(() => post(`/api/admin/users/${u.id}/provision`), "Provisioning queued")}>
                  Re-provision
                </Button>
                <Button
                  size="sm"
                  title="Re-check every mirror and recreate any deleted from Outlook"
                  onClick={() => act(() => post(`/api/admin/users/${u.id}/rebuild`), "Rebuild queued")}
                >
                  Rebuild mirrors
                </Button>
              </Td>
            </tr>
          ))}
        </Table>
      )}
    </div>
  );
}

function AssignMailbox({ user, onAssign }: { user: AdminUser; onAssign: (mailbox: string) => void }) {
  const [value, setValue] = useState(user.primary_mailbox ?? "");
  return (
    <form
      className="mt-2 flex gap-1.5"
      onSubmit={(e) => {
        e.preventDefault();
        if (value.trim()) onAssign(value.trim());
      }}
    >
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="mailbox@yourtenant.com"
        className="w-52 rounded-md border border-slate-300 px-2 py-1 text-xs"
      />
      <Button size="sm">Assign</Button>
    </form>
  );
}

// --- subscriptions / errors / audit ------------------------------------------------------

function Subscriptions() {
  const subs = useResource<AdminSubscription[]>("/api/admin/subscriptions", 10_000);
  const [queued, setQueued] = useState<string>();
  if (!subs.data) return subs.error ? <ErrorNote error={subs.error} /> : <Spinner />;
  return (
    <Table head={["Source", "Tenant", "Expires", "Renews", "Status", ""]}>
      {subs.data.map((s) => (
        <tr key={s.id}>
          <Td>
            <div className="font-medium text-slate-900">{s.source_email}</div>
            <div className="text-xs text-slate-500">for {s.user_email}</div>
            <div className="font-mono text-[11px] text-slate-400">{s.graph_subscription_id}</div>
          </Td>
          <Td>{s.tenant_name}</Td>
          <Td title={dateTime(s.expires_at)}>{relativeTime(s.expires_at)}</Td>
          <Td title={dateTime(s.renew_due_at)}>{relativeTime(s.renew_due_at)}</Td>
          <Td>
            <StatusBadge status={s.status} />
            {s.last_error && <div className="mt-1 max-w-64 text-xs text-rose-700">{s.last_error}</div>}
          </Td>
          <Td className="text-right">
            <Button
              size="sm"
              disabled={queued === s.source_id}
              onClick={async () => {
                await post(`/api/admin/sources/${s.source_id}/resync`);
                setQueued(s.source_id);
              }}
            >
              {queued === s.source_id ? "Queued" : "Full resync"}
            </Button>
          </Td>
        </tr>
      ))}
    </Table>
  );
}

function Errors() {
  const errors = useResource<SyncErrorRow[]>("/api/admin/errors?limit=200", 10_000);
  if (!errors.data) return errors.error ? <ErrorNote error={errors.error} /> : <Spinner />;
  if (errors.data.length === 0) return <Card className="p-8 text-center text-sm text-slate-500">No sync errors recorded.</Card>;
  return (
    <Table head={["When", "Kind", "Context", "Message"]}>
      {errors.data.map((e) => (
        <tr key={e.id}>
          <Td className="whitespace-nowrap">
            <span title={dateTime(e.created_at)}>{relativeTime(e.created_at)}</span>
          </Td>
          <Td>
            <code className="text-xs">{e.kind}</code>
          </Td>
          <Td className="text-xs text-slate-600">
            {[e.tenant_name, e.source_email, e.user_email].filter(Boolean).join(" · ") || "—"}
          </Td>
          <Td className="max-w-xl break-words text-xs text-rose-800">{e.message}</Td>
        </tr>
      ))}
    </Table>
  );
}

function AuditLog() {
  const audit = useResource<AuditRow[]>("/api/admin/audit?limit=200", 10_000);
  if (!audit.data) return audit.error ? <ErrorNote error={audit.error} /> : <Spinner />;
  return (
    <Table head={["When", "Actor", "Action", "Target", "Details"]}>
      {audit.data.map((a) => (
        <tr key={a.id}>
          <Td className="whitespace-nowrap">
            <span title={dateTime(a.created_at)}>{relativeTime(a.created_at)}</span>
          </Td>
          <Td className="text-xs">{a.actor}</Td>
          <Td>
            <code className="text-xs">{a.action}</code>
          </Td>
          <Td className="font-mono text-[11px] text-slate-500">
            {a.target_type}:{a.target_id?.slice(0, 8)}
          </Td>
          <Td className="max-w-md break-words font-mono text-[11px] text-slate-600">
            {Object.keys(a.details).length ? JSON.stringify(a.details) : ""}
          </Td>
        </tr>
      ))}
    </Table>
  );
}
