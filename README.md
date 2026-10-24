# Calendar Sync

Unified Microsoft 365 calendar: each user links the calendars they own in any number of
Microsoft 365 tenants, and the app integrates all of them, in near real time, into one
calendar inside the app. There is no primary calendar or account in a tenant we operate,
and nothing is written to any Microsoft 365 calendar. Source calendars are only read.

This implements [`docs/PRD Unified Microsoft 365 Calendar.md`](docs/PRD%20Unified%20Microsoft%20365%20Calendar.md).

Source calendars are read with **delegated** permissions. To link a calendar, a user signs in
to their own tenant, approves read-only calendar access for themselves, and is redirected back.
There is no tenant admin consent step. When a user's sign-in for a source expires or is
revoked, that source pauses (its events stay in the calendar) and the user is asked to reconnect.

## Architecture

```
 customer tenants                     our platform (docker compose)
┌──────────────────┐  change/     ┌──────────────┐  enqueue  ┌─────────┐   ┌──────────────┐
│ Microsoft Graph  │─ lifecycle ─▶│ api (FastAPI)│──────────▶│  redis  │──▶│ worker (arq) │
│ (Calendars.Read, │  webhooks    │ validate+202 │           │ (queue) │   │ fetch, delta,│
│  delegated)      │              │ REST: unified│           └────▲────┘   │ store, subs  │
└──────────────────┘              │ calendar     │                │        └──────┬───────┘
         ▲  reads (each linked    └──────┬───────┘                │               │
         │  user's delegated token,      │                        │               │
         │  refreshed per source)  ┌─────▼────────┐   ┌───────────┴───┐           │
         └─────────────────────────┤  postgres    │◀──┤ scheduler     │◀──────────┘
  browser ──▶ web (Next.js) ──────▶│ (events of   │   │ (arq cron)    │  renew subscriptions,
              /api, /auth proxied  │ every source)│   └───────────────┘  periodic delta
                                   └──────────────┘
```

| Service | Role |
| --- | --- |
| `web` | Next.js 16 app: sign-in, the unified **Calendar** (the home page), linked accounts with sync health, operator console. Proxies `/api/*` and `/auth/*` to the API so cookies are first-party. |
| `api` | FastAPI. Webhook + lifecycle receivers (validate `clientState`, enqueue, return 202), OAuth flows, REST API including `GET /api/calendar`, which builds the unified view from stored events. Never calls Graph while handling a notification. |
| `redis` | The queue (arq). AOF-persisted so queued jobs survive restarts. Also holds the per-source delegated access-token cache (refresh locks included), throttle back-off and job coalescing keys. |
| `worker` | Fetch-on-notify, `calendarView` delta sync, storing events, subscription create/renew/reauthorize, unlink clean-up, pausing sources that need reauthentication. Scale with `--scale worker=N`. |
| `scheduler` | Cron sweeps that only enqueue work: renewals (every 15 min, renew after 24 h) and the safety-net delta (every 6 h). |
| `db` | Postgres 17. Schema managed by Alembic (`migrate` runs on start). Holds the stored events behind every user's calendar. |
| `mockgraph` | **Dev only** (`COMPOSE_PROFILES=mock`): a stand-in for `login.microsoftonline.com` + Graph with a control panel, so the whole system runs without real tenants. |

There is no primary calendar and nothing is written to any Microsoft 365 calendar: the
app's Calendar page *is* the unified calendar. It integrates the calendars of all of a
user's linked accounts, refreshes every 10 seconds, and reads only from Postgres.

### How a change flows

1. Graph POSTs a change notification to `/webhooks/graph`. The API checks the
   subscription's `clientState` (encrypted at rest, constant-time compare) and tenant,
   enqueues `process_notification` and returns 202. Duplicate notifications for an event
   that is still queued collapse into one job.
2. A worker fetches the event with that source's delegated token (fetch-on-notify) and
   stores only the fields the calendar shows (subject and location encrypted; never
   attendees or body, FR-9), and only if Graph's `lastModifiedDateTime` is newer, so
   repeated or out-of-order notifications do nothing. Cancellations and deletes remove the
   stored event.
3. The next time the user's Calendar refreshes, `GET /api/calendar` groups the stored
   events of all their visible sources by `iCalUId` (the same meeting in two accounts shows
   once, FR-14), keeps the busiest show-as (FR-10), labels each entry with every source it
   came from, and applies each tenant's **Hide details** setting. Label and privacy changes
   therefore take effect immediately, with no rewrite.

Reliability: subscriptions are renewed well before the 4230-minute limit and recreated
when Graph says they're gone; `missed` / `subscriptionRemoved` trigger a delta resync; a
6-hourly delta (re-baselined daily so the 90-day-back / 365-day-ahead window keeps
rolling) treats delta as the source of truth; Graph 429/503 set a per-tenant back-off in
Redis honouring `Retry-After`, so one busy tenant never slows the others.

Delegated tokens: the link callback stores the source's refresh token (encrypted). Workers
redeem it at the source tenant's token endpoint, cache the access token per source in Redis,
persist rotated refresh tokens, and serialise refreshes per source with a Redis lock. When
a refresh fails with an error that needs the user (`invalid_grant`, `interaction_required`,
`consent_required`, AADSTS 70008 / 700082 / 50173 / 50076 / 65001 / …), or Graph keeps
returning 401/403, the source moves to `reauth_required`. Sync pauses, its already-synced
events stay in the calendar (marked *paused* in the legend), and the Accounts page shows a
**Reconnect** button (`/auth/reauth/{id}`, account pre-filled). After the user signs in
again as the same account, the subscription is recreated and a delta resync catches up.
Network errors, 5xx and 429 are retried.

## Quick start (local, against the mock)

Requirements: Docker with Compose v2.

```bash
make env            # or: cp .env.example .env and set SESSION_SECRET + DATA_ENCRYPTION_KEYS
docker compose up -d --build
```

| URL | |
| --- | --- |
| http://localhost:3000 | the app |
| http://localhost:8001 | mock Microsoft 365 control panel (create/move/cancel events, revoke a user's sign-in or consent, block user consent for a tenant, throttle a tenant, drop notifications, send lifecycle events) |
| http://localhost:8000/docs | API docs; `/metrics` for Prometheus (worker metrics on `:9100` inside the network) |

Walkthrough:

1. Sign in as **alice@contoso.example**. She lands on her (empty) **Calendar**.
   **Link an account** → `alice@contoso.example`: she approves read-only calendar access
   herself and existing events are backfilled into her Calendar. Link
   `alice@fabrikam.example` too. No admin step is needed for either tenant; both show up
   under **Operations → Tenants** (sign in as **ops@calsync.example**, an operator via
   `OPERATOR_EMAILS`).
2. In the mock control panel, create or move an event in a source mailbox and watch it
   appear in **Calendar** within a few seconds. The "Contoso x Fabrikam partnership"
   meeting is in both of Alice's calendars and is shown once, labelled with both.
3. In the panel, click **Revoke refresh tokens** for `alice@fabrikam.example`. Graph sends
   `reauthorizationRequired`, the refresh fails, and within seconds the source shows
   **sign-in needed** and the Accounts page asks her to reconnect; her Fabrikam events stay
   in the Calendar, marked paused.
   **Reconnect** signs her in again and sync catches up.
4. Use **Toggle blocking user consent** on a tenant, then link an account from it that
   hasn't consented yet: the result page tells the user their IT admin must approve the app.

Mock state persists in a volume; `make reset` (`docker compose down -v && up`) starts over.

## Tests

```bash
make test           # backend suite (85 tests) in the backend image + frontend typecheck/lint
make e2e            # 14 live checks against a fresh running stack (after `make reset`)
```

The backend suite runs in-process: the real FastAPI app and worker job functions,
real Postgres and Redis, and the mock Graph wired together over ASGI transports. It checks
sync behaviour through `GET /api/calendar` and the stored events: self-service linking
without admin consent, refresh-token storage and rotation, expired or revoked refresh
tokens pausing a source (events stay visible), reconnecting with the wrong and the right
account, tenants that block user consent, the source app being refused client
credentials, backfill, create/update/cancel/delete, duplicate and stale notifications,
cross-source de-duplication and busiest show-as, unlink, `missed` /
`subscriptionRemoved` / `reauthorizationRequired`, renewal and recreation, expired delta
tokens, pagination, per-tenant throttling, time zones, privacy mode in both directions,
user isolation, account deletion, the read-only guard (FR-11) and attendees/body never
being stored (FR-9).

Without `make`:

```bash
docker compose up -d db redis
docker compose run --rm --no-deps \
  -e TEST_DATABASE_URL=postgresql+asyncpg://calsync:calsync@db:5432/calsync_test \
  -e TEST_REDIS_URL=redis://redis:6379/15 api pytest -q
```

## Running against real Microsoft 365

1. **Source app** (in our home tenant): *Accounts in any organizational directory*.
   API permissions, all **Delegated**: `openid`, `profile`, `email`, `offline_access`,
   `User.Read`, `Calendars.Read`. None need admin consent and there are no application
   permissions; don't click "Grant admin consent" for customer tenants. Redirect URI (Web):
   `{PUBLIC_WEB_URL}/auth/callback` (used for sign-in, linking and reconnecting). Add a
   certificate (preferred) or a secret. Complete publisher verification: tenants on
   Microsoft's recommended consent setting only let users consent to verified publishers.
   Tenants that disable user consent need their admin to approve the app (users are told
   so); the platform itself never asks for admin consent. This is the **only** app
   registration: there is no write app and no mailbox in our tenant.
2. In `.env`: remove `COMPOSE_PROFILES`, `LOGIN_BASE_URL`, `LOGIN_BROWSER_BASE_URL` and
   `GRAPH_BASE_URL`; set `SOURCE_APP_CLIENT_ID` and its credentials (`SOURCE_APP_CERT_PATH`
   + `SOURCE_APP_CERT_THUMBPRINT` for a certificate), `PUBLIC_WEB_URL`, and
   `PUBLIC_API_URL`, a **public HTTPS** URL that routes `/webhooks/*` to the `api` service
   (for local testing, a tunnel such as `cloudflared` or `ngrok` to port 8000).
3. Put the services behind TLS. Keep `.env` secrets in your secret store (e.g. Key Vault,
   injected as env vars or files), and rotate `DATA_ENCRYPTION_KEYS` by prepending a new key.

### Upgrading from the primary-calendar version

Migration `0003_remove_primary_calendars` drops `users.primary_*` and `mirror_mappings`.

- Drain the queue before deploying: the `reconcile_group`, `rebuild_user` and
  `provision_primary` jobs no longer exist.
- Mirrors already written to the old primary calendars are **not** deleted (the app that
  could write there is gone). Delete those calendars (or the shared mailbox / per-user
  mailboxes), then remove the primary app registration, its secret and its application
  access policy in Entra.
- Remove `HOME_TENANT_ID`, `PRIMARY_APP_*`, `PRIMARY_CALENDAR_MODE`,
  `PRIMARY_SHARED_MAILBOX` and `MIRROR_LABEL_IN_SUBJECT` from `.env` (they are ignored).
  Accounts in our own tenant can now be linked like any other.
- Locally against the mock, run `make reset`: the mock's old host tenant is gone.
- Downgrading recreates the columns and sets every user to `provisioning`, so the old code
  would create fresh primary calendars; the pre-upgrade calendars aren't reattached.

## PRD coverage

| Req | Where |
| --- | --- |
| FR-1 tenant registered on first link, no admin consent | `api/auth.py::_complete_link` |
| FR-2 one unified calendar per user, in the app | `api/me.py::unified_calendar`, `frontend/app/calendar` |
| FR-3 delegated sign-in proves ownership | `api/auth.py::_complete_link` (OIDC `tid` + `oid`, nonce + browser-bound state; refresh token stored encrypted) |
| FR-4 subscription per source calendar | `sync/subscriptions.py::create_subscription` |
| FR-5 webhook + fetch-on-notify | `api/webhooks.py`, `sync/source_sync.py::process_notification` |
| FR-6 renew before expiry | `worker/sweeps.py::renew_sweep`, `subscriptions.renew_subscription` |
| FR-7 delta on link, on missed, on schedule | `sync/source_sync.py::sync_source`, `delta_sweep` |
| FR-8 events labelled by source | `api/me.py::unified_calendar` (label editable per source, applied on read) |
| FR-9 no attendees or body stored | `sync/events.py` normalisation; tested |
| FR-10 keep show-as | `sync/events.py::busiest` (busiest status wins across duplicates) |
| FR-11 never write to sources | `GraphClient` refuses every non-GET call except `/subscriptions`; tested |
| FR-12 remove a source's events on unlink | `lifecycle.purge_source` |
| FR-13 per-user and per-tenant health | Accounts page, Operations console, Prometheus metrics labelled by tenant |
| FR-14 same meeting in two sources shown once | `unified_calendar` groups by `iCalUId` (`dedup_key`) |
| FR-15 pause and ask to reconnect on expired/revoked sign-in | `graph/tokens.py` (classification), `lifecycle.mark_reauth_required`, Accounts banner |
| FR-16 reconnect resumes with delta resync | `api/auth.py` (`/auth/reauth/{id}`, same `tid`/`oid` enforced), `setup_source` recovery mode |
| FR-17 "ask your IT admin" when user consent is blocked | `api/auth.py` callback → `/link/result?status=admin_approval_required` |

## Decisions on the PRD's open questions

- **Where the unified calendar lives**: in the app. The earlier primary calendar in a
  tenant we operate (and its app-only `Calendars.ReadWrite` app) was removed.
- **Fields stored**: subject, time, time zone, location, show-as, sensitivity. Body and
  attendees are never read into storage. A per-tenant **"Hide details"** switch shows that
  tenant's events as "Busy" with no location, for customers that forbid showing subjects.
- Defaults follow the PRD's proposals: renew every 24 h, delta every 6 h, window 90 days
  back / 365 ahead. All are configurable in `.env`.

## Not done yet / known gaps

- The unified calendar is only visible in the app (week view). There is no ICS feed or
  other way to see it in Outlook or on a phone (PRD open question).
- Alerting: metrics are exported per tenant (`calsync_delta_drift_total`,
  `calsync_subscription_renewal_failures_total`, …), but no alert rules or dashboards ship.
- The Azure Event Hubs delivery fallback isn't implemented.
- The mock only simulates single-instance events. Recurring series are handled in code
  (occurrences come from `calendarView`, series-master changes trigger a delta) but only
  exercised by unit-level logic, not by the mock.
- Deleting data after unlink or account deletion is immediate rather than after a
  configurable retention window. Sources paused for reauthentication keep their events
  indefinitely (PRD open question).
- Users are only prompted to reconnect in the app; there are no email reminders yet.
- Existing deployments still on `0001`: migration `0002_delegated_auth` has no refresh
  tokens to carry over, so every existing linked source becomes `reauth_required` and each
  user must reconnect once. See also *Upgrading from the primary-calendar version* above.
- Real Entra error codes for blocked consent and revoked grants are classified from
  Microsoft's documentation and tested against the mock only.
- The audit log is in Postgres, with no export.

## Repository layout

```
backend/
  app/api/          FastAPI routers (webhooks, auth, user API, operator API)
  app/graph/        delegated token service (per source), read-only Graph client, OIDC
  app/sync/         normalisation, delta, notifications, subscriptions, lifecycle
  app/worker/       arq job wrappers (retry/throttle/reauth), cron sweeps, settings
  alembic/          migrations
  mockgraph/        local Microsoft identity + Graph stand-in (dev/test only)
  scripts/          e2e smoke test
  tests/            in-process integration + unit tests
frontend/           Next.js app (App Router, Tailwind)
docker-compose.yml
```
