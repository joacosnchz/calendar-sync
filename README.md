# Calendar Sync

Unified Microsoft 365 calendar: each user links the calendars they own in any number of
Microsoft 365 tenants, and the service mirrors those events, in near real time, into that
user's primary calendar in a tenant we operate. Source calendars are never written to.

This implements [`docs/PRD Unified Microsoft 365 Calendar.md`](docs/PRD%20Unified%20Microsoft%20365%20Calendar.md).

## Architecture

```
 customer tenants                     our platform (docker compose)                       our tenant
┌──────────────────┐  change/     ┌──────────────┐  enqueue  ┌─────────┐   ┌──────────────┐   ┌───────────────┐
│ Microsoft Graph  │─ lifecycle ─▶│ api (FastAPI)│──────────▶│  redis  │──▶│ worker (arq) │──▶│ primary       │
│ (Calendars.Read) │  webhooks    │ validate+202 │           │ (queue) │   │ fetch, delta,│   │ calendars     │
└──────────────────┘              └──────┬───────┘           └────▲────┘   │ mirror, subs │   │ (ReadWrite    │
         ▲  reads (app-only token per tenant)  │                  │        └──────┬───────┘   │  app, scoped) │
         └─────────────────────────────────────┼──────────────────┼───────────────┘           └───────────────┘
                                   ┌───────────▼──┐   ┌───────────┴───┐
  browser ──▶ web (Next.js) ──────▶│  postgres    │   │ scheduler     │  renew subscriptions, periodic
              /api, /auth proxied  └──────────────┘   │ (arq cron)    │  delta, mirror audit
                                                      └───────────────┘
```

| Service | Role |
| --- | --- |
| `web` | Next.js 16 app: sign-in, linked accounts with sync health, unified week view, operator console. Proxies `/api/*` and `/auth/*` to the API so cookies are first-party. |
| `api` | FastAPI. Webhook + lifecycle receivers (validate `clientState`, enqueue, return 202), OAuth flows, REST API. Never calls Graph while handling a notification. |
| `redis` | The queue (arq). AOF-persisted so queued jobs survive restarts. Also holds the per-tenant token cache, throttle back-off and job coalescing keys. |
| `worker` | Fetch-on-notify, `calendarView` delta sync, mirror writes, subscription create/renew/reauthorize, unlink/revocation clean-up. Scale with `--scale worker=N`. |
| `scheduler` | Cron sweeps that only enqueue work: renewals (every 15 min, renew after 24 h), safety-net delta (every 6 h), mirror audit, stuck provisioning. |
| `db` | Postgres 17. Schema managed by Alembic (`migrate` runs on start). |
| `mockgraph` | **Dev only** (`COMPOSE_PROFILES=mock`): a stand-in for `login.microsoftonline.com` + Graph with a control panel, so the whole system runs without real tenants. |

### How a change flows

1. Graph POSTs a change notification to `/webhooks/graph`. The API checks the
   subscription's `clientState` (encrypted at rest, constant-time compare) and tenant,
   enqueues `process_notification` and returns 202. Duplicate notifications for an event
   that is still queued collapse into one job.
2. A worker fetches the event with the tenant's app-only token (fetch-on-notify), stores
   only the fields mirrors need (subject and location encrypted), and only if Graph's
   `lastModifiedDateTime` is newer, so repeated or out-of-order notifications do nothing.
3. A `reconcile_group` job brings the user's primary calendar in line with every copy of
   that meeting (grouped by `iCalUId`, FR-14). The mirror has no attendees (FR-9), keeps
   show-as (FR-10), is labelled by source, and is written in the original time zone.
   Creates send a stored `transactionId`, so a retried create never makes a second mirror.

Reliability: subscriptions are renewed well before the 4230-minute limit and recreated
when Graph says they're gone; `missed` / `subscriptionRemoved` trigger a delta resync; a
6-hourly delta (re-baselined daily so the 90-day-back / 365-day-ahead window keeps
rolling) treats delta as the source of truth; Graph 429/503 set a per-tenant (reads) or
per-mailbox (writes) back-off in Redis honouring `Retry-After`, so one busy tenant never
slows the others. Specific AADSTS errors (Enterprise app deleted, consent missing) mark
the tenant revoked and remove its mirrors; transient errors are retried.

## Quick start (local, against the mock)

Requirements: Docker with Compose v2.

```bash
make env            # or: cp .env.example .env and set SESSION_SECRET + DATA_ENCRYPTION_KEYS
docker compose up -d --build
```

| URL | |
| --- | --- |
| http://localhost:3000 | the app |
| http://localhost:8001 | mock Microsoft 365 control panel (create/move/cancel events, revoke consent, throttle a tenant, drop notifications, send lifecycle events) |
| http://localhost:8000/docs | API docs; `/metrics` for Prometheus (worker metrics on `:9100` inside the network) |

Walkthrough:

1. Sign in as **ops@calsync.example** (an operator via `OPERATOR_EMAILS`). Under
   **Operations → Tenants**, create "Contoso" and open the consent link, then accept as
   Contoso's admin.
2. Sign in as **alice@contoso.example**. Her unified calendar is provisioned
   automatically. **Link an account** → `alice@contoso.example`: existing events are
   backfilled. Link `alice@fabrikam.example` too: Fabrikam hasn't consented, so the page
   gives her a link to forward to that admin. Once it's approved, syncing starts on its own.
3. In the mock control panel, create or move an event in a source mailbox and watch it
   appear in **Calendar** (and in the host tenant's shared mailbox in the panel) in about a
   second. The "Contoso x Fabrikam partnership" meeting is in both of Alice's calendars and
   is mirrored once.

Mock state persists in a volume; `make reset` (`docker compose down -v && up`) starts over.

## Tests

```bash
make test           # backend suite (68 tests) in the backend image + frontend typecheck/lint
make e2e            # 14 live checks against a fresh running stack (after `make reset`)
```

The backend suite runs in-process: the real FastAPI app and worker job functions,
real Postgres and Redis, and the mock Graph wired together over ASGI transports. It covers
onboarding, consent and denial, backfill, create/update/cancel/delete, duplicate and
stale notifications, cross-source de-duplication, unlink, tenant revocation and
re-consent, `missed` / `subscriptionRemoved` / `reauthorizationRequired`, renewal and
recreation, expired delta tokens, pagination, per-tenant throttling, time zones, privacy
mode, user isolation, account deletion, the read-only guard (FR-11) and "no attendees on
mirrors" (FR-9).

Without `make`:

```bash
docker compose up -d db redis
docker compose run --rm --no-deps \
  -e TEST_DATABASE_URL=postgresql+asyncpg://calsync:calsync@db:5432/calsync_test \
  -e TEST_REDIS_URL=redis://redis:6379/15 api pytest -q
```

## Running against real Microsoft 365

1. **Source app** (in our home tenant): *Accounts in any organizational directory*.
   API permissions: `Calendars.Read` (**Application**) plus delegated `openid`, `profile`,
   `email`, `User.Read`. Redirect URIs (Web): `{PUBLIC_WEB_URL}/auth/callback` and
   `{PUBLIC_WEB_URL}/auth/adminconsent/callback`. Add a certificate (preferred) or a secret.
   Complete publisher verification.
2. **Primary app** (single tenant): `Calendars.ReadWrite` (**Application**), admin consent
   in our tenant, then restrict it to the primary-calendar mailboxes:
   ```powershell
   New-ApplicationAccessPolicy -AppId <primary-app-id> -PolicyScopeGroupId calendars@yourtenant.com `
     -AccessRight RestrictAccess -Description "Calendar Sync primary calendars"
   ```
3. **Primary calendars**: `PRIMARY_CALENDAR_MODE=shared_mailbox` creates one calendar per
   user in `PRIMARY_SHARED_MAILBOX`. `user_mailbox` uses a licensed mailbox per user that an
   operator assigns in the console.
4. In `.env`: remove `COMPOSE_PROFILES`, `LOGIN_BASE_URL`, `LOGIN_BROWSER_BASE_URL` and
   `GRAPH_BASE_URL`; set the app IDs and credentials (`*_CERT_PATH` for certificates),
   `HOME_TENANT_ID`, `PUBLIC_WEB_URL`, and `PUBLIC_API_URL`, a **public HTTPS** URL that
   routes `/webhooks/*` to the `api` service (for local testing, a tunnel such as
   `cloudflared` or `ngrok` to port 8000).
5. Put the services behind TLS. Keep `.env` secrets in your secret store (e.g. Key Vault,
   injected as env vars or files), and rotate `DATA_ENCRYPTION_KEYS` by prepending a new key.

## PRD coverage

| Req | Where |
| --- | --- |
| FR-1 admin consent link per customer | Operations → Tenants; `api/admin.py`, `api/auth.py::admin_consent_callback` |
| FR-2 one primary calendar per user | `sync/lifecycle.py::provision_primary` (both hosting models) |
| FR-3 link only mailboxes you sign in to | `api/auth.py::_complete_link` (OIDC `tid` + `oid`, nonce + browser-bound state) |
| FR-4 subscription per source calendar | `sync/subscriptions.py::create_subscription` |
| FR-5 webhook + fetch-on-notify | `api/webhooks.py`, `sync/source_sync.py::process_notification` |
| FR-6 renew before expiry | `worker/sweeps.py::renew_sweep`, `subscriptions.renew_subscription` |
| FR-7 delta on link, on missed, on schedule | `sync/source_sync.py::sync_source`, `delta_sweep` |
| FR-8 mirror labelled by source | `sync/mirror.py` (subject prefix + category; label editable per source) |
| FR-9 no attendees | `build_mirror_payload`; tested |
| FR-10 keep show-as | `build_mirror_payload` (busiest status wins across duplicates) |
| FR-11 never write to sources | `GraphClient` refuses non-GET source calls except `/subscriptions`; tested |
| FR-12 remove mirrors on unlink / revoke | `lifecycle.purge_source`, `revoke_tenant` |
| FR-13 per-user and per-tenant health | Accounts page, Operations console, Prometheus metrics labelled by tenant |
| FR-14 same meeting in two sources mirrored once | groups by `iCalUId` (`dedup_key`) |

## Decisions on the PRD's open questions

- **Primary calendar hosting**: both models are implemented (`PRIMARY_CALENDAR_MODE`); the
  default is a calendar per user in one shared mailbox (no licence per user).
- **Fields mirrored**: subject, time, time zone, location, show-as, sensitivity, plus a
  source label. Body and attendees are never read into storage. A per-tenant **"Hide
  details"** switch mirrors that tenant's events as "Busy" with no location, for customers
  that forbid copying subjects.
- Defaults follow the PRD's proposals: renew every 24 h, delta every 6 h, window 90 days
  back / 365 ahead. All are configurable in `.env`.

## Not done yet / known gaps

- **How users open their primary calendar** (still an open question): sharing the
  per-user calendar to the user's own work account isn't automated.
- Alerting: metrics are exported per tenant (`calsync_delta_drift_total`,
  `calsync_subscription_renewal_failures_total`, …), but no alert rules or dashboards ship.
- Writes are throttled per mailbox on 429, but there's no proactive per-mailbox
  concurrency cap.
- The Azure Event Hubs delivery fallback isn't implemented.
- The mock only simulates single-instance events. Recurring series are handled in code
  (occurrences come from `calendarView`, series-master changes trigger a delta) but only
  exercised by unit-level logic, not by the mock.
- Deleting data after unlink, revocation or account deletion is immediate rather than
  after a configurable retention window.
- The audit log is in Postgres, with no export.

## Repository layout

```
backend/
  app/api/          FastAPI routers (webhooks, auth, user API, operator API)
  app/graph/        token service, Graph client (throttling, read-only guard), OIDC
  app/sync/         normalisation, delta, notifications, mirroring, subscriptions, lifecycle
  app/worker/       arq job wrappers (retry/throttle/revocation), cron sweeps, settings
  alembic/          migrations
  mockgraph/        local Microsoft identity + Graph stand-in (dev/test only)
  scripts/          e2e smoke test
  tests/            in-process integration + unit tests
frontend/           Next.js app (App Router, Tailwind)
docker-compose.yml
```
