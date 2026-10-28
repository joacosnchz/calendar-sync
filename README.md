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

The backend and the database run on **Supabase**: Edge Functions (TypeScript on Deno) and
Postgres, with pg_cron for the periodic work. The web app is Next.js. There is no queue:
whoever receives a change (a Graph webhook, the sweep, the link flow) reads it from Graph
and writes it straight to the database.

```
 customer tenants                Supabase                                       
┌──────────────────┐ change/   ┌───────────────────────┐ writes ┌──────────────────────┐
│ Microsoft Graph  │─lifecycle▶│ webhooks function     │───────▶│ Postgres             │
│ (Calendars.Read, │ webhooks  │ verify, 202, then     │        │ events of every      │
│  delegated)      │◀──────────│ fetch + store         │        │ source, tokens,      │
└──────────────────┘ reads     └───────────────────────┘        │ subscriptions, audit │
         ▲  (each linked user's  ┌───────────────────────┐ reads  │                      │
         │  delegated token)     │ api + auth functions  │◀──────▶│                      │
         └───────────────────────│ OAuth, REST, unified  │        │ pg_cron ─every min──┐│
                                 │ calendar              │        └─────────────────────┼┘
  browser ──▶ web (Next.js) ────▶└───────────────────────┘        ┌───────────────────▼──┐
              /api, /auth proxied                                 │ cron function: renew,│
                                                                  │ delta, retries       │
                                                                  └──────────────────────┘
```

| Piece | Role |
| --- | --- |
| `web` | Next.js 16 app: sign-in, the unified **Calendar** (the home page), linked accounts with sync health, operator console. Proxies `/api/*` and `/auth/*` to the Edge Functions of the same names so cookies are first-party. |
| `api` / `auth` functions | One Hono app: OAuth flows (`/auth/*`) and the REST API (`/api/*`), including `GET /api/calendar`, which builds the unified view from stored events. Linking a calendar subscribes and backfills it in the background (`EdgeRuntime.waitUntil`). |
| `webhooks` function | Graph change and lifecycle receivers. Validates `clientState` and tenant, answers 202 at once, then fetches the event and writes it to the database after the response. |
| `cron` function | The sweep, called by pg_cron every minute: renews subscriptions before they expire, recreates missing ones, runs the safety-net delta and any resync that couldn't run at the time, and finishes interrupted unlinks and account deletions. |
| Postgres | Schema in `supabase/migrations`. Every table has RLS on and no policies, and the Data API roles have no privileges: only the functions (connecting directly) can read it. Also holds the per-source access-token cache, per-tenant Graph throttling, the per-source sync lease and pending resync requests (Redis's job before). |
| `mockgraph` | **Dev/test only** (`COMPOSE_PROFILES=mock`): a stand-in for `login.microsoftonline.com` + Graph with a control panel, so the whole system runs without real tenants. |

There is no primary calendar and nothing is written to any Microsoft 365 calendar: the
app's Calendar page *is* the unified calendar. It integrates the calendars of all of a
user's linked accounts, refreshes every 10 seconds, and reads only from Postgres.

### How a change flows

1. Graph POSTs a change notification to `{PUBLIC_API_URL}/webhooks/graph`. The function
   checks the subscription's `clientState` (encrypted at rest, constant-time compare) and
   tenant, and returns 202. Duplicate notifications for one event in a delivery collapse
   into one fetch.
2. After the response, the function fetches the event with that source's delegated token
   (fetch-on-notify) and stores only the fields the calendar shows (subject and location
   encrypted; never attendees or body, FR-9), and only if Graph's `lastModifiedDateTime` is
   newer, so repeated or out-of-order notifications do nothing. Cancellations and deletes
   remove the stored event.
3. The next time the user's Calendar refreshes, `GET /api/calendar` groups the stored
   events of all their visible sources by `iCalUId` (the same meeting in two accounts shows
   once, FR-14), keeps the busiest show-as (FR-10), labels each entry with every source it
   came from, and applies each tenant's **Hide details** setting. Label and privacy changes
   therefore take effect immediately, with no rewrite.

Reliability: subscriptions are renewed well before the 4230-minute limit and recreated
when Graph says they're gone; `missed` / `subscriptionRemoved` trigger a delta resync; a
6-hourly delta (re-baselined daily so the 90-day-back / 365-day-ahead window keeps
rolling) treats delta as the source of truth. Graph 429/503 set a per-tenant back-off in
Postgres honouring `Retry-After`, so one busy tenant never slows the others. Short
throttles (each `Retry-After` ≤ 30 s, up to a minute in total) are waited out in place;
longer ones, and network/5xx errors that persist after three attempts, are recorded as a
resync request that the sweep runs within a minute. One delta runs per source at a time
(a lease on the source row); a sync asked for while another runs is left to the sweep.

Delegated tokens: the link callback stores the source's refresh token (encrypted). The
functions redeem it at the source tenant's token endpoint, cache the access token
(encrypted) on the source row, persist rotated refresh tokens, and serialise refreshes per
source with a row lock (`SELECT … FOR UPDATE`). When a refresh fails with an error that
needs the user (`invalid_grant`, `interaction_required`, `consent_required`, AADSTS 70008 /
700082 / 50173 / 50076 / 65001 / …), or Graph keeps returning 401/403, the source moves to
`reauth_required`. Sync pauses, its already-synced events stay in the calendar (marked
*paused* in the legend), and the Accounts page shows a **Reconnect** button
(`/auth/reauth/{id}`, account pre-filled). After the user signs in again as the same
account, the subscription is recreated and a delta resync catches up.

## Quick start (local, against the mock)

Requirements: Docker, `make`, the [Supabase CLI](https://supabase.com/docs/guides/local-development/cli/getting-started),
Node 24 (frontend checks) and Python 3 (`make env` uses it to generate secrets). Deno isn't
needed locally: tests and checks run it in a container.

```bash
make env            # writes .env (compose) and supabase/functions/.env (functions, fresh secrets)
make up             # build the images, supabase start (database, functions, pg_cron), web + mock
```

`make build` uses `docker build --network host` (not `docker compose build`) because on some
WSL setups BuildKit's default build network has no DNS.

| URL | |
| --- | --- |
| http://localhost:3000 | the app |
| http://localhost:8001 | mock Microsoft 365 control panel (create/move/cancel events, revoke a user's sign-in or consent, block user consent for a tenant, throttle a tenant, drop notifications, send lifecycle events) |
| http://localhost:54323 | Supabase Studio (tables, logs, cron jobs) |

`supabase start` applies the migrations and `supabase/seed.sql` (the Vault secrets pg_cron
uses to reach the `cron` function), and serves every function in `supabase/functions` with
the settings in `supabase/functions/.env`. Function logs: `make logs`.

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
   in the Calendar, marked paused. **Reconnect** signs her in again and sync catches up.
4. Use **Toggle blocking user consent** on a tenant, then link an account from it that
   hasn't consented yet: the result page tells the user their IT admin must approve the app.

`make reset` wipes the database and the mock and starts again with demo events.

Working on the frontend outside Docker: `cd frontend && npm run dev` proxies to
`http://localhost:54321/functions/v1` by default (`SUPABASE_FUNCTIONS_URL`).

## Tests

```bash
make test           # Deno suite (86 tests) + frontend typecheck/lint; needs `supabase start`
make lint           # deno lint / fmt --check / check, ruff for the mock
make e2e            # 14 live checks against a fresh running stack (after `make reset`)
```

The Deno suite (`supabase/functions/tests`) runs the real Hono apps and sync code
in-process against a dedicated `calsync_test` database on the local Supabase Postgres
(rebuilt from the migration on every run) and a throwaway mock on port 8002 that delivers
real webhooks back to the test process. It checks sync behaviour through `GET
/api/calendar` and the stored events: self-service linking without admin consent,
refresh-token storage and rotation, expired or revoked refresh tokens pausing a source
(events stay visible), reconnecting with the wrong and the right account, tenants that
block user consent, the source app being refused client credentials, backfill,
create/update/cancel/delete, duplicate and stale notifications, cross-source
de-duplication and busiest show-as, unlink, `missed` / `subscriptionRemoved` /
`reauthorizationRequired`, renewal and recreation, expired delta tokens, pagination and
batched writes, per-tenant throttling (waited out, or left to the sweep), the sync lease,
interrupted unlinks and deletions, time zones, privacy mode in both directions, user
isolation, account deletion, the read-only guard (FR-11), attendees/body never being stored
(FR-9), the cron secret, and the tables being closed to Supabase's Data API. Unit tests
also check that data encrypted and sessions issued by the previous Python backend still
read.

`make e2e` drives the full stack (Next.js proxy, Supabase gateway, the functions in the
edge runtime, pg_cron) the way a browser would.

## Deploying to a hosted Supabase project

1. **Link the project** and push the schema (migrations only; `seed.sql` is local-only):

   ```bash
   supabase link --project-ref <project-ref>
   supabase db push
   ```

2. **Function secrets.** Write a file with the production values from
   `supabase/functions/.env.example`, *without* `DATABASE_URL` (Supabase provides
   `SUPABASE_DB_URL`; set `DATABASE_URL` only to use the transaction pooler on port 6543)
   and *without* `PUBLIC_API_URL` (it defaults to `https://<project-ref>.supabase.co/functions/v1`),
   then:

   ```bash
   supabase secrets set --env-file prod.env
   ```

   Generate `SESSION_SECRET`, `DATA_ENCRYPTION_KEYS` and `CRON_SECRET` freshly (or carry
   the first two over when migrating, see below). Keep the file out of the repository.

3. **Vault secrets for the sweep.** In the SQL editor:

   ```sql
   select vault.create_secret('https://<project-ref>.supabase.co/functions/v1', 'calsync_functions_url');
   select vault.create_secret('<the CRON_SECRET value>', 'calsync_cron_secret');
   ```

   Until both exist, the scheduled job logs a warning and does nothing. Check it with
   `select * from cron.job_run_details order by start_time desc limit 5;`.

4. **Deploy the functions** (JWT verification stays off as set in `supabase/config.toml`:
   each function authenticates its own callers):

   ```bash
   supabase functions deploy --use-api      # or: make deploy (also runs db push)
   ```

5. **Web app.** Build it with `SUPABASE_FUNCTIONS_URL=https://<project-ref>.supabase.co/functions/v1`
   (a Docker build arg; rewrites are fixed at build time) and host it behind TLS at
   `PUBLIC_WEB_URL`.

Notes for hosted projects:

- Edge Functions limit each invocation's wall-clock time (background work included; about
  150 s on the free plan, 400 s on paid plans). Delta pages are written in batches, so a
  typical backfill takes seconds; if one is cut short the sweep runs it again from the
  last stored delta link.
- `SUPABASE_DB_URL` is a direct connection. For many concurrent webhooks, point
  `DATABASE_URL` at the transaction pooler (`…pooler.supabase.com:6543`); the client
  doesn't use prepared statements, so it works with it.
- Logs: **Edge Functions → Logs** in the dashboard, and the operator console in the app.

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
2. In the function secrets (`supabase/functions/.env` locally): remove `LOGIN_BASE_URL`,
   `LOGIN_BROWSER_BASE_URL` and `GRAPH_BASE_URL`; set `SOURCE_APP_CLIENT_ID` and its
   credentials (`SOURCE_APP_CERT_PEM`, a PEM with the PKCS#8 private key and certificate,
   optionally `SOURCE_APP_CERT_THUMBPRINT`; or `SOURCE_APP_CLIENT_SECRET`) and
   `PUBLIC_WEB_URL`. Graph must reach `{PUBLIC_API_URL}/webhooks/*` over **public HTTPS**:
   automatic on a hosted project; locally, a tunnel (`cloudflared`, `ngrok`) to port 54321
   with `PUBLIC_API_URL=https://<tunnel>/functions/v1`. In `.env`, drop the `mock` profile.
3. Rotate `DATA_ENCRYPTION_KEYS` by prepending a new key (all listed keys decrypt).

## Upgrading from the FastAPI/Redis version

The schema is the old Alembic head (`0003`) plus new nullable or defaulted columns, and
encryption (Fernet) and session cookies (HS256) are unchanged, so the data carries over:

1. Stop the old `api`, `worker` and `scheduler` (the Redis queue isn't needed any more; jobs
   still in it are dropped, and the sweep catches up on anything they would have done).
2. Copy the data, then drop the old subscriptions, whose notification URL points at the old
   API:

   ```bash
   pg_dump --data-only --exclude-table=alembic_version "$OLD_DATABASE_URL" > data.sql
   psql "$SUPABASE_DATABASE_URL" -c "set session_replication_role = replica" -f data.sql
   psql "$SUPABASE_DATABASE_URL" -c "delete from subscriptions"
   ```

3. Set `SESSION_SECRET` and `DATA_ENCRYPTION_KEYS` to the old values, so stored tokens and
   events decrypt and users stay signed in.
4. The next sweep (within a minute) creates a new subscription for every active source and
   resyncs it. The old subscriptions expire at Graph within three days.

Removed: the Prometheus metrics (`/metrics`, worker `:9100`). Use the function logs
(`delta drift`, `renewal failed`, `graph throttled`, `task … failed`), the operator
console, `sync_errors` and `audit_log` instead.

## PRD coverage

All paths are under `supabase/functions/_shared/`.

| Req | Where |
| --- | --- |
| FR-1 tenant registered on first link, no admin consent | `api/auth.ts::completeLink` |
| FR-2 one unified calendar per user, in the app | `api/me.ts` (`GET /api/calendar`), `frontend/app/calendar` |
| FR-3 delegated sign-in proves ownership | `api/auth.ts::completeLink` (OIDC `tid` + `oid`, nonce + browser-bound state; refresh token stored encrypted) |
| FR-4 subscription per source calendar | `sync/subscriptions.ts::createSubscription` |
| FR-5 webhook + fetch-on-notify | `api/webhooks.ts`, `sync/source_sync.ts::processNotification` |
| FR-6 renew before expiry | `sync/sweeps.ts::sweep`, `subscriptions.ts::renewSubscription` |
| FR-7 delta on link, on missed, on schedule | `sync/source_sync.ts::syncSource`, `sync/sweeps.ts` |
| FR-8 events labelled by source | `api/me.ts` (label editable per source, applied on read) |
| FR-9 no attendees or body stored | `events.ts` normalisation; tested |
| FR-10 keep show-as | `events.ts::busiest` (busiest status wins across duplicates) |
| FR-11 never write to sources | `GraphClient` refuses every non-GET call except `/subscriptions`; tested |
| FR-12 remove a source's events on unlink | `sync/lifecycle.ts::purgeSource` |
| FR-13 per-user and per-tenant health | Accounts page, Operations console, `sync_errors`, function logs labelled by tenant |
| FR-14 same meeting in two sources shown once | `GET /api/calendar` groups by `iCalUId` (`dedup_key`) |
| FR-15 pause and ask to reconnect on expired/revoked sign-in | `graph/tokens.ts` (classification), `lifecycle.ts::markReauthRequired`, Accounts banner |
| FR-16 reconnect resumes with delta resync | `api/auth.ts` (`/auth/reauth/{id}`, same `tid`/`oid` enforced), `setupSource` recovery mode |
| FR-17 "ask your IT admin" when user consent is blocked | `api/auth.ts` callback → `/link/result?status=admin_approval_required` |

## Decisions on the PRD's open questions

- **Where the unified calendar lives**: in the app. The earlier primary calendar in a
  tenant we operate (and its app-only `Calendars.ReadWrite` app) was removed.
- **Fields stored**: subject, time, time zone, location, show-as, sensitivity. Body and
  attendees are never read into storage. A per-tenant **"Hide details"** switch shows that
  tenant's events as "Busy" with no location, for customers that forbid showing subjects.
- Defaults follow the PRD's proposals: renew every 24 h, delta every 6 h, window 90 days
  back / 365 ahead. All are configurable in the function secrets.

## Not done yet / known gaps

- The unified calendar is only visible in the app (week view). There is no ICS feed or
  other way to see it in Outlook or on a phone (PRD open question).
- Alerting: there are no metrics or alert rules any more, only logs, `sync_errors` and the
  operator console.
- A full re-baseline of a source has to finish within one function invocation's time limit
  (see *Deploying*). A calendar too large for that would keep restarting its re-baseline;
  there is no resumable re-baseline yet.
- The Azure Event Hubs delivery fallback isn't implemented.
- The mock only simulates single-instance events. Recurring series are handled in code
  (occurrences come from `calendarView`, series-master changes trigger a delta) but only
  exercised by unit-level logic, not by the mock.
- Deleting data after unlink or account deletion is immediate rather than after a
  configurable retention window. Sources paused for reauthentication keep their events
  indefinitely (PRD open question).
- Users are only prompted to reconnect in the app; there are no email reminders yet.
- Real Entra error codes for blocked consent and revoked grants are classified from
  Microsoft's documentation and tested against the mock only.
- The audit log is in Postgres, with no export.

## Repository layout

```
supabase/
  config.toml           local stack + per-function settings (JWT verification off)
  migrations/           schema, RLS lockdown, pg_cron sweep schedule
  seed.sql              local-only Vault secrets for the sweep
  functions/
    api/ auth/ webhooks/ cron/   entry points (one Hono app each; api and auth share one)
    _shared/api/        routers: user API, operator API, OAuth, webhooks, cron
    _shared/graph/      delegated token service (per source), read-only Graph client, OIDC
    _shared/sync/       fetch-on-notify, delta, subscriptions, lifecycle, task runner, sweep
    _shared/            config, Postgres, Fernet, sessions, event normalisation, time zones
    tests/              Deno unit + in-process integration tests
frontend/               Next.js app (App Router, Tailwind)
mockgraph/              local Microsoft identity + Graph stand-in, e2e smoke test (dev/test only)
docker-compose.yml      web app + mock
```
