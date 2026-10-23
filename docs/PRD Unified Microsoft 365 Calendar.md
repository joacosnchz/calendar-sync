# PRD: Unified Microsoft 365 Calendar

Oct 22, 2026 · @Joaquin

## Overview

We are building a service that connects to Microsoft 365 calendars across many tenants and merges them into one unified calendar. It uses Microsoft Graph change notifications (webhooks) so changes appear in near real time, without polling.

People who work across several organizations (consultants, agencies, holding companies, board members) keep a separate calendar in each tenant. They double-book, miss conflicts, and manually copy events between calendars. Microsoft 365 has no native way to see calendars from unrelated tenants in one place.

The product is multi-user. Each user links the calendars they own, in any number of tenants, and the service mirrors those events into that user's own primary calendar in a Microsoft 365 tenant we operate. Users then see everything in one calendar in Outlook. Source calendars are never written to.

## Goals and non-goals

**Goals**

- Let many users each unify their own set of source calendars into their own primary calendar.
- Connect calendars from any number of Microsoft 365 tenants through one multi-tenant app registration.
- Reflect event creates, updates and deletes in the primary calendar within seconds of the change.
- Never silently lose sync: detect missed notifications and expired subscriptions, then recover automatically.
- Let tenant admins grant, scope and revoke access on their own terms.

**Non-goals (v1)**

- Non-Microsoft calendars (Google, iCloud, CalDAV).
- Writing to source calendars. v1 is strictly read-only toward sources; it writes only to primary calendars in our tenant.
- Editing events from the primary calendar back into their source.
- Scheduling features such as booking pages or meeting polls.

**Success metrics**

| Metric | Target |
| --- | --- |
| Change-to-display latency (p95) | Open question: proposed under 60 s |
| Subscriptions renewed before expiry | 100% |
| Events missing after delta resync | 0 |
| Time to onboard a new tenant | Open question: proposed under 10 min with an admin present |

## Users and use cases

| User | Need |
| --- | --- |
| End user (works across tenants) | Link the calendars they own and see all of their meetings in one primary calendar. |
| Tenant admin (customer IT) | Approve the app, limit which mailboxes it can read, and revoke access at any time. |
| Operator (our team) | Provision primary calendars, monitor subscription health, and troubleshoot sync gaps. |

Core use cases:

1. An admin in a new tenant grants consent once, which makes that tenant available to every user who works there.
2. A user signs up, gets a primary calendar in our tenant, and links source accounts by signing in to each one.
3. A user creates, moves or cancels a meeting in any linked source, and their primary calendar updates.
4. A user opens their primary calendar and sees every meeting, with overlaps visible as in any Outlook calendar.
5. A user unlinks a source, or an admin revokes the app, and the mirrored events from that source are removed.

## Functional requirements

| ID | Requirement | Priority |
| --- | --- | --- |
| FR-1 | Onboard a tenant through an admin consent link generated per customer. | Must |
| FR-2 | Provision one primary calendar per user in our tenant. | Must |
| FR-3 | Let each user link the source mailboxes they own, proving ownership by signing in to that account. | Must |
| FR-4 | Create a Graph subscription per linked source calendar for `created`, `updated` and `deleted` changes. | Must |
| FR-5 | Receive webhook notifications and fetch the full event from Graph (fetch-on-notify). | Must |
| FR-6 | Renew every subscription before it expires. | Must |
| FR-7 | Run a delta query per source calendar on linking, on missed notifications, and on a schedule. | Must |
| FR-8 | Mirror every source event into the owning user's primary calendar, labelled by source. | Must |
| FR-9 | Create mirrored events without attendees, so no invitations or responses are ever sent. | Must |
| FR-10 | Keep each mirrored event's show-as status (busy, tentative, free) so free/busy is accurate. | Must |
| FR-11 | Never write to a source calendar. | Must |
| FR-12 | Remove mirrored events when a user unlinks a source or a tenant revokes consent. | Must |
| FR-13 | Show per-user and per-tenant sync health (last notification, subscription expiry, errors). | Should |
| FR-14 | When one meeting appears in two of a user's sources, mirror it once. | Should |

## Architecture

&#91;embedded content: system architecture · 7 components\]

The webhook receiver only validates and queues. All Graph reads happen in the workers, so a slow fetch never delays the reply Graph is waiting for.

## Authentication and tenant onboarding

One app registration in our home tenant serves every customer tenant. Each customer admin consents once, and we request tokens per tenant.

**App registration**

- Supported account types: "Accounts in any organizational directory" (multi-tenant).
- Permission: `Calendars.Read` as an *application* permission, so sync runs unattended without a signed-in user.
- Credentials: a certificate (preferred) or client secret, stored only in our home tenant and our secret store. Customer tenants never see it.
- Publisher verification: complete it by linking a Microsoft Partner ID. Many tenants block consent to unverified apps, and verification builds admin trust.

**Writing to primary calendars (our tenant)**

- Use a second, single-tenant app registration in our tenant with `Calendars.ReadWrite` (application). Keeping write access in a separate app means customer admins only ever consent to `Calendars.Read`.
- Scope it with an application access policy (or RBAC for Applications) to the mailboxes that hold primary calendars.

**Onboarding flow**

1. We send the customer admin a consent link: `https://login.microsoftonline.com/{tenant-id}/adminconsent?client_id={app-id}&redirect_uri={redirect-uri}`
2. The admin approves. This creates a service principal (Enterprise application) for our app in their tenant.
3. Our redirect endpoint records the tenant ID and marks the tenant as connected.
4. Optionally, the admin restricts which mailboxes we can read using an Exchange application access policy or RBAC for Applications.

**User account linking**

Admin consent makes a tenant available, but each user still links only their own mailboxes.

1. The user signs in to the source account through a delegated OpenID Connect flow (`openid`, `profile`, `User.Read`).
2. We read the signed-in user's object ID and tenant ID, which proves they own that mailbox.
3. We store the link (our user → source tenant + source user ID) and start syncing it with the app-only token.
4. A user can never link a mailbox they haven't signed in to, even within a consented tenant.

**Tokens**

- Use the client credentials flow against the target tenant's endpoint, never `common`: `https://login.microsoftonline.com/{tenant-id}/oauth2/v2.0/token`
- A token only works for its own tenant, so the token service caches one token per tenant and refreshes it before expiry.

**Revocation**

- A customer admin can remove access at any time by deleting the Enterprise application.
- We detect this through token failures and lifecycle notifications, then mark the tenant disconnected (FR-11).

## Change notifications (webhooks)

We subscribe to each synced calendar with Microsoft Graph change notifications, so Graph calls our endpoint whenever an event is created, updated or deleted.

**Creating a subscription** (with that tenant's token)

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

**Lifecycle notifications** (sent to `lifecycleNotificationUrl`)

| Event | What it means | Our response |
| --- | --- | --- |
| `reauthorizationRequired` | Access must be re-confirmed soon | Call `POST /subscriptions/{id}/reauthorize` or renew |
| `subscriptionRemoved` | Graph removed the subscription | Recreate it, then delta resync |
| `missed` | Some notifications were not delivered | Delta resync for that calendar |

**Delta query resync**

- Use `GET /users/{id}/calendarView/delta?startDateTime=…&endDateTime=…` and store the returned `deltaLink` per calendar.
- Run it on first connection (initial backfill), after any `missed` or `subscriptionRemoved` event, and on a periodic safety schedule (proposed: every 6 hours).
- Delta results are the source of truth when they disagree with notification-driven state.

**Throttling**

- Respect Graph `429` responses and `Retry-After` headers with backoff per tenant, so one busy tenant doesn't slow the rest.

## Data model and sync logic

Every event is keyed by tenant, mailbox and Graph event ID, so the same ID in two tenants never collides.

| Entity | Key fields |
| --- | --- |
| User | user\_id, email, primary\_calendar (mailbox + calendar ID in our tenant), created\_at |
| Tenant | tenant\_id, display name, consent status, connected\_at, revoked\_at |
| Linked source | user\_id, tenant\_id, source\_user\_id, calendar\_id, delta\_link, last\_synced\_at, status |
| Subscription | subscription\_id, linked source, client\_state (encrypted), expires\_at, status |
| Source event | linked source + Graph event ID, subject, start, end, time zone, location, show\_as, is\_all\_day, iCalUId, last\_modified |
| Mirror mapping | source event key → mirrored event ID in the user's primary calendar, last\_written\_at |

**Sync rules**

- **Create or update:** fetch the source event, store it if Graph's `lastModifiedDateTime` is newer, then create or update the mirrored event via the mirror mapping.
- **Delete:** on a `deleted` notification or delta `@removed` entry, delete the mirrored event and the mapping.
- **No attendees on mirrors:** mirrored events carry subject, time, location, show-as and a source label, but no attendee list, so Exchange never sends invitations.
- **Recurring events:** mirror individual occurrences from `calendarView` over a rolling window (proposed: 90 days back, 365 ahead), so exceptions and cancellations stay correct.
- **Duplicates within one user:** if the same meeting appears in two of a user's sources, group by `iCalUId` and mirror it once.
- **Isolation between users:** two users can link the same source mailbox only if both proved ownership; each gets their own mirror.
- **Time zones:** store times in UTC plus the original time zone, and write mirrors with the original time zone.

## Security, privacy and compliance

- **Least privilege:** customer tenants grant `Calendars.Read` only. Write access (`Calendars.ReadWrite`) exists only in our own tenant, in a separate app scoped to primary-calendar mailboxes.
- **Proof of ownership:** a user can link a source mailbox only after signing in to it. Admin consent alone never exposes a mailbox to a user.
- **Secrets:** keep certificates or client secrets in a managed vault (e.g. Azure Key Vault) and rotate them on a schedule.
- **Webhook trust:** validate `clientState` on every notification before processing.
- **Isolation:** every query is scoped by user and tenant. One user's events must never be mirrored into another user's primary calendar.
- **Data at rest:** encrypt event data, and store only the fields the mirror needs (no event body or attendee list).
- **Retention and deletion:** delete a user's mirrors and stored data within an agreed window after unlinking, account deletion or tenant revocation.
- **Compliance:** event data is personal data under GDPR. We need a DPA with customers, a documented data location, and audit logs of access.

## Non-functional requirements

| Area | Requirement |
| --- | --- |
| Webhook response | Validation reply within 10 s; notification acknowledged with 202 in under 3 s |
| Availability | Webhook endpoint highly available (proposed 99.9%), since Graph retries failed deliveries only for a limited time before dropping notifications |
| Scalability | Handle many users, each with several linked sources across many tenants (one subscription per source calendar); workers scale horizontally from the queue |
| Observability | Metrics for notifications received, fetch errors, renewal failures and delta drift, with alerts per tenant |
| Recovery | Any calendar can be fully rebuilt from a fresh delta query |

## Milestones and rollout

&#91;embedded content: phased rollout · 4 phases, 3 gates\]

Each gate must pass before the next phase starts. Dates are an open question until the team sizes each phase.

## Risks and open questions

| Risk | Mitigation |
| --- | --- |
| Customer admins refuse consent to an external app | Publisher verification, read-only permission, mailbox scoping via access policies |
| Silent sync gaps from expired subscriptions or dropped notifications | Proactive renewal, lifecycle notifications, scheduled delta resync |
| Graph throttling on reads in large tenants | Per-tenant backoff, delta instead of full fetches |
| Graph throttling on writes into our tenant (limits apply per mailbox) | Queue writes per primary mailbox, batch requests, back off on `429` |
| Mirrors accidentally send invitations | Never set attendees on mirrored events (FR-9), covered by automated tests |
| Cost of a mailbox per user in our tenant | Decide the primary-calendar hosting model early (open question below) |
| Notification storms (e.g. bulk updates) | Queue buffering and de-duplication by event ID |

Open questions:

- [x] Is v1 read-only? Yes: strictly read-only toward source calendars.
- [x] Where is the unified calendar shown? In one primary calendar per user, hosted in a tenant we operate.
- [x] Rich notifications or fetch-on-notify? Fetch-on-notify is good enough.
- [ ] How is each primary calendar hosted: a licensed mailbox per user, or a calendar inside a shared mailbox?
- [ ] How do users open their primary calendar: an account in our tenant, or the calendar shared to their own work account?
- [ ] Which event fields may we mirror, and do any customers forbid copying meeting subjects?
- [ ] What are the latency and onboarding-time targets in the success metrics?
