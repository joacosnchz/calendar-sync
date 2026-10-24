# PRD: Unified Microsoft 365 Calendar

Oct 22, 2026 · @Joaquin · Updated Oct 24, 2026: delegated permissions, no admin consent; the unified calendar lives in the app (no primary calendar)

## Overview

We are building a service that connects to Microsoft 365 calendars across many tenants and merges them into one unified calendar. It uses Microsoft Graph change notifications (webhooks) so changes appear in near real time, without polling.

People who work across several organizations (consultants, agencies, holding companies, board members) keep a separate calendar in each tenant. They double-book, miss conflicts, and manually copy events between calendars. Microsoft 365 has no native way to see calendars from unrelated tenants in one place.

The product is multi-user. Each user links the calendars they own, in any number of tenants, and the service integrates the events from all of those linked accounts into one unified calendar inside our app. There is no separate primary calendar or account in a tenant we operate, and the service writes to no Microsoft 365 calendar at all. Source calendars are never written to.

Linking is self-service. To link a calendar, the user is sent to their own tenant's Microsoft sign-in page, approves read-only calendar access for themselves, and is redirected back. No tenant admin has to approve anything first. The service reads each calendar with that user's delegated permission. If that permission lapses, the service asks the user to sign in again instead of failing silently.

## Goals and non-goals

**Goals**

- Let many users each unify their own set of source calendars into their own calendar in the app.
- Connect calendars from any number of Microsoft 365 tenants through one multi-tenant app registration.
- Let users link a calendar on their own, by signing in to their tenant and consenting for themselves, with no tenant admin consent step.
- Reflect event creates, updates and deletes in the unified calendar within seconds of the change.
- Never silently lose sync: detect missed notifications and expired subscriptions, then recover automatically. When a user's sign-in expires or is revoked, ask that user to sign in again.
- Leave tenant admins in control through their usual Entra ID tools (user consent policy, Enterprise application settings, Conditional Access), without asking them to do anything to get started.

**Non-goals (v1)**

- Non-Microsoft calendars (Google, iCloud, CalDAV).
- Writing to any Microsoft 365 calendar. v1 is strictly read-only toward sources, and there is no primary calendar to write to.
- Showing the unified calendar in Outlook or other calendar clients (for example by mirroring it into a mailbox or publishing an ICS feed).
- Editing events from the unified calendar back into their source.
- Scheduling features such as booking pages or meeting polls.

**Success metrics**

| Metric | Target |
| --- | --- |
| Change-to-display latency (p95) | Open question: proposed under 60 s |
| Subscriptions renewed before expiry | 100% |
| Events missing after delta resync | 0 |
| Time to link a calendar from a new tenant | Open question: proposed under 2 min, with no admin involved |
| Sources waiting on reauthentication for more than 7 days | Open question: proposed under 5% |

## Users and use cases

| User | Need |
| --- | --- |
| End user (works across tenants) | Link the calendars they own by signing in to each one, see all of their meetings in one calendar in the app, and reconnect when asked. |
| Tenant admin (customer IT) | Nothing is required to get started. Optionally, they can control whether users may consent, restrict which users can use the app, and revoke access at any time. |
| Operator (our team) | Monitor subscription and sign-in health, and troubleshoot sync gaps. |

Core use cases:

1. A user signs up and links source accounts by signing in to each one in its own tenant. The first link from a tenant makes that tenant known to us. No admin step is needed.
2. A user creates, moves or cancels a meeting in any linked source, and their unified calendar updates.
3. A user opens the calendar in the app and sees every meeting from every linked account, labelled by account, with overlaps visible side by side.
4. A user's access to a source lapses, for example from a password reset, an expired sign-in, or a new MFA policy. The platform pauses that source and asks the user to reconnect. After the user signs in again, sync catches up automatically.
5. A user unlinks a source, and that source's events disappear from the unified calendar.
6. A user's organization doesn't allow users to consent to apps. The platform tells the user to ask their IT admin for approval.

## Functional requirements

| ID | Requirement | Priority |
| --- | --- | --- |
| FR-1 | Register a tenant automatically the first time one of its users links a calendar. No admin consent link or tenant invite is used. | Must |
| FR-2 | Give each user one unified calendar in the app that integrates the calendars of all their linked accounts. Nothing is provisioned in any Microsoft 365 tenant. | Must |
| FR-3 | Let each user link the source mailboxes they own through a delegated sign-in to their own tenant. The user consents to read-only calendar access, and the sign-in proves ownership. | Must |
| FR-4 | Create a Graph subscription per linked source calendar for `created`, `updated` and `deleted` changes, using that user's delegated access. | Must |
| FR-5 | Receive webhook notifications and fetch the full event from Graph (fetch-on-notify). | Must |
| FR-6 | Renew every subscription before it expires. | Must |
| FR-7 | Run a delta query per source calendar on linking, on missed notifications, and on a schedule. | Must |
| FR-8 | Show every source event in the owning user's unified calendar, labelled by source. | Must |
| FR-9 | Store only the fields the calendar shows. Never read attendees or event bodies into storage. | Must |
| FR-10 | Keep each event's show-as status (busy, tentative, free) so free/busy is accurate. | Must |
| FR-11 | Never write to a source calendar. | Must |
| FR-12 | Remove a source's events from the unified calendar, and its stored data, when a user unlinks it. | Must |
| FR-13 | Show per-user and per-tenant sync health (last notification, subscription expiry, sign-in status, errors). | Should |
| FR-14 | When one meeting appears in two of a user's sources, show it once. | Should |
| FR-15 | When a source's refresh token is expired, revoked, or needs fresh sign-in (MFA, Conditional Access), mark the source "reauthentication required", pause its sync, keep its already-synced events in the calendar, and ask the user to reconnect. | Must |
| FR-16 | Let the user reconnect a paused source by signing in to the same account. On success, resume sync, recreate the subscription if needed, and catch up with a delta resync. | Must |
| FR-17 | If the user's tenant blocks user consent, tell the user that IT admin approval is needed, instead of showing a generic error. | Should |

## Architecture

&#91;embedded content: system architecture · 7 components\]

The webhook receiver only validates and queues. All Graph reads happen in the workers, so a slow fetch never delays the reply Graph is waiting for.

## Authentication and account linking

One multi-tenant app registration in our home tenant serves every customer tenant. It reads source calendars with **delegated** permissions, acting as the user who linked each calendar. No application permissions are requested from customer tenants, and no tenant-wide admin consent is required. Each user consents for themselves, for their own mailbox only.

**App registration (source app, multi-tenant)**

- Supported account types: "Accounts in any organizational directory" (multi-tenant, work and school accounts).
- Delegated permissions: `openid`, `profile`, `email`, `offline_access`, `User.Read` and `Calendars.Read`. Microsoft marks none of them as requiring admin consent, so users can consent themselves wherever their tenant's consent policy allows it.
- No application permissions.
- Redirect URI: a single web redirect, `{web-url}/auth/callback`, used for sign-in, linking and reconnecting.
- Credentials: a certificate (preferred) or client secret, because the app is a confidential client. They are stored only in our home tenant and our secret store.
- Publisher verification: complete it by linking a Microsoft Partner ID. This matters more with user consent than it did with admin consent. Many tenants, including those on Microsoft's recommended default policy, let users consent only to apps from verified publishers.

**No write access anywhere**

- The unified calendar is stored and shown by the app itself, so there is no second app registration, no `Calendars.ReadWrite` permission and no mailbox in our own tenant. The source app above is the only app registration.

**Platform sign-in**

- Users sign in to the platform with their work account through OpenID Connect (`openid`, `profile`, `email`, `User.Read`). This asks only for the basic sign-in permissions, so it never triggers a calendar consent prompt.
- When the platform session expires, the user is sent back through the same sign-in.

**Linking a source calendar** (FR-3)

1. The user chooses "Link a calendar". The platform redirects them to Microsoft's `organizations` authorize endpoint, requesting `openid profile email offline_access User.Read Calendars.Read` (incremental consent).
2. The user signs in to their own tenant and, on first use, approves "Read your calendars" for themselves. On the first consent in a tenant, Entra creates our app's service principal (Enterprise application) there automatically.
3. Microsoft redirects back to `/auth/callback` with an authorization code. The platform exchanges it for an ID token, an access token and a refresh token.
4. The ID token's `tid` and `oid` prove which tenant and mailbox the user owns. The platform stores the link (our user → source tenant + source user ID) together with the encrypted refresh token, registers the tenant if it is new (FR-1), then subscribes and backfills.
5. The user can only ever link a mailbox they signed in to. Delegated `Calendars.Read` reaches nothing but the signed-in user's own calendars.

**When the tenant blocks user consent** (FR-17)

- Some tenants turn off user consent, or limit it to verified publishers or to selected low-impact permissions. In that case Microsoft shows a "Need admin approval" screen. If the tenant has enabled the admin consent workflow, the user can request approval from that screen.
- When Microsoft returns the user with a consent error (for example `AADSTS90094`), the platform explains that their IT admin must approve the app. Once the admin approves through their normal Entra process, the user links again.
- The platform never generates admin consent links and never depends on an admin approving anything. Admin approval is the tenant's own choice.

**Tokens**

- Access tokens are per linked source. Each worker gets one by redeeming that source's refresh token at the source tenant's endpoint, never `common`: `https://login.microsoftonline.com/{tenant-id}/oauth2/v2.0/token` (`grant_type=refresh_token`, scope `Calendars.Read offline_access`).
- Access tokens are cached per source and refreshed before expiry. Refresh tokens are encrypted at rest. When Microsoft returns a rotated refresh token, the platform stores it. A per-source lock stops concurrent workers from racing on rotation.
- Refresh tokens last up to 90 days and are renewed whenever they are used, so regular syncing keeps them alive. They still end on password resets, admin or user revocation, inactivity, Conditional Access sign-in frequency, and new MFA or consent requirements.

**Reauthentication** (FR-15, FR-16)

1. When a refresh fails with an error that needs user interaction, the source moves to `reauth_required`. Examples are `invalid_grant`, `interaction_required` and `consent_required` (AADSTS70008, 700082, 50173, 50076, 50079, 65001 and similar). Network errors and Microsoft 5xx responses are retried instead.
2. Sync for that source pauses: renewals, delta queries and notification processing skip it. Its already-synced events stay in the unified calendar, so the user's view is stale but not empty.
3. The dashboard shows a banner and a Reconnect button on the source. Reconnect sends the user to that tenant's sign-in page with their account pre-filled (`login_hint`).
4. The returning `tid`/`oid` must match the paused source. A different account is rejected with a clear message.
5. On success, the platform stores the new refresh token, reactivates the source, recreates or renews its subscription, and runs a delta resync to pick up everything that changed while it was paused.

**Admin control and revocation**

- Tenant admins need to do nothing for users to start. If they want control, they use standard Entra ID settings: the user consent policy, "Assignment required" on our Enterprise application, Conditional Access, or the admin consent workflow.
- An admin or user can remove access at any time, for example by revoking the user's consent or sessions, or by deleting the Enterprise application. The platform sees this as a failed refresh and moves the affected sources to `reauth_required` (FR-15). The user can then reconnect, if their tenant still allows it, or unlink.
- With delegated access, there is no tenant-wide access to revoke and no application access policy to configure. The app can only read mailboxes whose owners have signed in.

## Change notifications (webhooks)

We subscribe to each synced calendar with Microsoft Graph change notifications, so Graph calls our endpoint whenever an event is created, updated or deleted.

**Creating a subscription** (with the linking user's delegated token)

```http
POST https://graph.microsoft.com/v1.0/subscriptions
{
  "changeType": "created,updated,deleted",
  "notificationUrl": "https://api.example.com/webhook",
  "lifecycleNotificationUrl": "https://api.example.com/lifecycle",
  "resource": "users/{user-id}/events",
  "expirationDateTime": "<now + under 3 days>",
  "clientState": "<random secret per subscription>"
}
```

**Requirements for the webhook receiver**

- **Validation handshake:** on subscription creation, Graph calls the URL with a `validationToken` query parameter. Respond within 10 seconds with HTTP 200, `text/plain`, and the token as the body.
- **Fast acknowledgement:** return 202 quickly and push the notification onto a queue. Fetching and processing happen in workers.
- **clientState check:** reject any notification whose `clientState` doesn't match the stored secret for that subscription.
- **Tenant routing:** each notification carries `tenantId` and `subscriptionId`. One shared endpoint serves all tenants.
- **Idempotency:** Graph may send duplicates or out-of-order notifications. Processing must be safe to repeat.

**Payload handling**

- By default a notification contains only the change type and the event's resource ID. Workers then call `GET /users/{id}/events/{event-id}` to fetch details.
- For deletes, the event can't be fetched; we remove it by ID.
- Rich notifications (`includeResourceData`) can carry event data in the payload, but are out of scope: fetch-on-notify is good enough for this product.

**Alternative delivery**

- If a public HTTPS endpoint is a problem, Graph can deliver notifications to Azure Event Hubs instead. Kept as a fallback option.

## Subscription lifecycle and reliability

Webhooks alone are not reliable enough: subscriptions expire within 3 days and notifications can be dropped. Renewal, lifecycle handling and delta resync close those gaps.

**Expiration and renewal**

- Outlook event subscriptions last at most 4,230 minutes (about 2.9 days).
- A scheduler renews each subscription with `PATCH /subscriptions/{id}` well before expiry (proposed: every 24 hours).
- If renewal fails because the subscription no longer exists, recreate it and run a delta resync for that calendar.
- Renewal, reauthorization and recreation all use the source's delegated token. If that token can't be refreshed, the source pauses until the user reconnects (FR-15). Graph stops delivering for it in the meantime, and the delta resync after reconnecting closes the gap.

**Lifecycle notifications** (sent to `lifecycleNotificationUrl`)

| Event | What it means | Our response |
| --- | --- | --- |
| `reauthorizationRequired` | Access must be re-confirmed soon (the delegated access token behind the subscription is expiring) | Refresh the source's delegated token, then call `POST /subscriptions/{id}/reauthorize` or renew. If the refresh fails, mark the source `reauth_required` and ask the user to reconnect (FR-15) |
| `subscriptionRemoved` | Graph removed the subscription | Recreate it, then delta resync |
| `missed` | Some notifications were not delivered | Delta resync for that calendar |

**Delta query resync**

- Use `GET /users/{id}/calendarView/delta?startDateTime=…&endDateTime=…` and store the returned `deltaLink` per calendar.
- Run it on first connection (initial backfill), after any `missed` or `subscriptionRemoved` event, after a user reconnects a paused source, and on a periodic safety schedule (proposed: every 6 hours). The schedule also keeps each refresh token in regular use.
- Delta results are the source of truth when they disagree with notification-driven state.

**Throttling**

- Respect Graph `429` responses and `Retry-After` headers with backoff per tenant, so one busy tenant doesn't slow the rest.

## Data model and sync logic

Every event is keyed by tenant, mailbox and Graph event ID, so the same ID in two tenants never collides.

| Entity | Key fields |
| --- | --- |
| User | user\_id, email, created\_at |
| Tenant | tenant\_id, display name, domain, copy\_details, created\_at (first link from the tenant) |
| Linked source | user\_id, tenant\_id, source\_user\_id, calendar\_id, refresh\_token (encrypted), delta\_link, last\_synced\_at, status (`active`, `reauth_required`, `removing`, `error`), reauth\_required\_at |
| Subscription | subscription\_id, linked source, client\_state (encrypted), expires\_at, status |
| Source event | linked source + Graph event ID, subject, start, end, time zone, location, show\_as, is\_all\_day, iCalUId, last\_modified |

**Sync rules**

- **Create or update:** fetch the source event and store it if Graph's `lastModifiedDateTime` is newer. The unified calendar is built from stored events when it is opened, so the change shows up on the next refresh.
- **Delete:** on a `deleted` notification or delta `@removed` entry, delete the stored event.
- **No attendees:** stored events carry subject, time, location, show-as and sensitivity, but no attendee list or body.
- **Recurring events:** store individual occurrences from `calendarView` over a rolling window (proposed: 90 days back, 365 ahead), so exceptions and cancellations stay correct.
- **Duplicates within one user:** if the same meeting appears in two of a user's sources, group by `iCalUId` and show it once, labelled with every source it came from. The busiest show-as wins.
- **Isolation between users:** two users can link the same source mailbox only if both proved ownership; each gets their own copy of its events.
- **Time zones:** store times in UTC plus the original time zone, and show them in the viewer's time zone.

## Security, privacy and compliance

- **Least privilege:** customer tenants grant only delegated `Calendars.Read` (plus sign-in scopes), one user at a time. The app has no tenant-wide or application permissions in customer tenants and can read only the mailboxes of users who signed in and consented. The platform has no write access to any calendar.
- **Proof of ownership:** a user can link a source mailbox only by signing in to it. The delegated token itself can't reach any other mailbox.
- **Secrets:** keep certificates or client secrets in a managed vault (e.g. Azure Key Vault) and rotate them on a schedule.
- **Refresh tokens:** encrypt them at rest with rotating keys, never log them or send them to the browser, and delete a source's token when the source is unlinked or its user is deleted.
- **Webhook trust:** validate `clientState` on every notification before processing.
- **Isolation:** every query is scoped by user and tenant. One user's events must never appear in another user's calendar.
- **Data at rest:** encrypt event data, and store only the fields the calendar shows (no event body or attendee list).
- **Retention and deletion:** delete a user's stored data within an agreed window after unlinking or account deletion. A source paused for reauthentication keeps its events until the user reconnects or unlinks. How long to keep them is an open question below.
- **Compliance:** event data is personal data under GDPR. We need a DPA with customers, a documented data location, and audit logs of access.

## Non-functional requirements

| Area | Requirement |
| --- | --- |
| Webhook response | Validation reply within 10 s; notification acknowledged with 202 in under 3 s |
| Availability | Webhook endpoint highly available (proposed 99.9%), since Graph retries failed deliveries only for a limited time before dropping notifications |
| Scalability | Handle many users, each with several linked sources across many tenants (one subscription per source calendar); workers scale horizontally from the queue |
| Observability | Metrics for notifications received, fetch errors, renewal failures, token refresh failures, sources waiting on reauthentication, and delta drift, with alerts per tenant |
| Recovery | Any calendar can be fully rebuilt from a fresh delta query |

## Milestones and rollout

&#91;embedded content: phased rollout · 4 phases, 3 gates\]

Each gate must pass before the next phase starts. Dates are an open question until the team sizes each phase.

## Risks and open questions

| Risk | Mitigation |
| --- | --- |
| Tenants that disable or restrict user consent block self-service linking | Publisher verification (required under Microsoft's recommended user consent setting), read-only delegated permission only, and a clear "ask your IT admin" message that points to the admin consent workflow (FR-17) |
| Refresh tokens expire or are revoked (password reset, MFA or Conditional Access changes, 90-day inactivity), pausing sync | Detect on refresh, pause the source without losing its events, prompt with a dashboard banner and a one-click Reconnect, then delta resync on return (FR-15, FR-16). Regular syncing keeps tokens from going inactive |
| Users ignore the reconnect prompt, so part of the unified calendar goes stale | Mark paused sources clearly. Email reminders and a retention limit are open questions below |
| Stored refresh tokens are a high-value secret | Encryption at rest with key rotation, never sent to the browser, deleted on unlink |
| Silent sync gaps from expired subscriptions or dropped notifications | Proactive renewal, lifecycle notifications, scheduled delta resync |
| Graph throttling on reads in large tenants | Per-tenant backoff, delta instead of full fetches |
| Users expect to see the unified calendar in Outlook or on their phone | v1 shows it only in the app. An ICS feed or similar is an open question below |
| Notification storms (e.g. bulk updates) | Queue buffering and de-duplication by event ID |

Open questions:

- [x] Is v1 read-only? Yes: strictly read-only toward source calendars.
- [x] Where is the unified calendar shown? In the app itself. The earlier design (one primary calendar per user in a tenant we operate, written by a separate app-only `Calendars.ReadWrite` app) was dropped, so there is nothing to host, license or share.
- [x] Rich notifications or fetch-on-notify? Fetch-on-notify is good enough.
- [ ] Should users also be able to subscribe to their unified calendar from Outlook or a phone (for example a private ICS feed)?
- [ ] Which event fields may we store and show, and do any customers forbid copying meeting subjects?
- [ ] What are the latency and onboarding-time targets in the success metrics?
- [x] Application or delegated permissions for source calendars? Delegated. Each user signs in to their own tenant and consents for themselves, so no tenant admin consent is required and unattended app-only access isn't needed.
- [ ] Should we also email users when a source needs reconnecting, or is the in-app banner enough for v1?
- [ ] How long should a source stay paused before we remove its events (proposed: 30 days)?
