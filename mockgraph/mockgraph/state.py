"""In-memory directory, calendars, subscriptions and notification delivery."""

import asyncio
import base64
import json
import logging
import os
import pickle
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from mockgraph.tz import resolve_tz

log = logging.getLogger("mockgraph")

MAX_SUBSCRIPTION_MINUTES = 4230
REFRESH_TOKEN_DAYS = 90
# Scopes that are part of every sign-in and never need consent on their own.
OIDC_SCOPES = {"openid", "profile", "email", "offline_access"}


def now_utc() -> datetime:
    return datetime.now(UTC)


def graph_dt(dt: datetime) -> str:
    # Graph uses 7 fractional digits.
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0"


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_any(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    if "." in value:
        head, rest = value.split(".", 1)
        digits = len(rest) - len(rest.lstrip("0123456789"))
        value = f"{head}.{rest[:digits][:6].ljust(6, '0')}{rest[digits:]}"
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_utc(value: dict[str, str], all_day: bool) -> datetime:
    naive = parse_any(value["dateTime"]).replace(tzinfo=None)
    if all_day:
        return naive.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)
    tz = resolve_tz(value.get("timeZone") or "UTC") or UTC
    return naive.replace(tzinfo=tz).astimezone(UTC)


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def unb64(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


class MockGraphError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


@dataclass
class MockUser:
    oid: str
    tenant_id: str
    upn: str
    display_name: str
    default_calendar_id: str = ""
    # Bumped by "revoke sessions": refresh tokens and delegated access tokens issued
    # before it stop working (Graph enforces it immediately, like Continuous Access
    # Evaluation does for user revocation).
    session_generation: int = 0


@dataclass
class MockTenant:
    tenant_id: str
    name: str
    domain: str
    users: dict[str, MockUser] = field(default_factory=dict)
    throttle_remaining: int = 0
    drop_notifications: bool = False
    # Entra "Do not allow user consent": users can't grant new delegated permissions.
    user_consent_blocked: bool = False


@dataclass
class MockRefreshToken:
    oid: str
    tenant_id: str
    client_id: str
    scopes: list[str]
    generation: int
    expires_at: datetime


@dataclass
class MockCalendar:
    id: str
    owner_oid: str
    name: str
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    changes: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class MockSubscription:
    id: str
    app_id: str
    tenant_id: str
    owner_oid: str
    calendar_id: str
    resource: str
    notification_url: str
    lifecycle_url: str | None
    client_state: str | None
    expiration: datetime
    change_types: set[str]
    # Delegated subscriptions belong to the signed-in user's grant: when it's revoked,
    # Graph stops delivering until the subscription is reauthorized or recreated.
    creator_oid: str | None = None
    creator_generation: int = 0


@dataclass
class Delivery:
    at: datetime
    kind: str
    url: str
    status: str
    detail: str


def _tid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"tenant.{name}"))


def _oid(upn: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"user.{upn}"))


class MockState:
    def __init__(
        self,
        *,
        source_app_id: str,
        source_app_secret: str,
        token_secret: str = "mockgraph-signing-key-not-secret-0123456789",
        inline_delivery: bool = False,
        notify_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.source_app_id = source_app_id
        self.source_app_secret = source_app_secret
        self.token_secret = token_secret
        self.inline_delivery = inline_delivery
        self.notifier = httpx.AsyncClient(transport=notify_transport, timeout=10.0)
        self.tenants: dict[str, MockTenant] = {}
        self.users: dict[str, MockUser] = {}
        self.calendars: dict[str, MockCalendar] = {}
        self.subscriptions: dict[str, MockSubscription] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, MockRefreshToken] = {}
        # Delegated permissions each user granted to each app: (oid, client_id) -> scopes.
        self.grants: dict[tuple[str, str], set[str]] = {}
        self.delta_sessions: dict[str, list[str]] = {}
        self.deliveries: list[Delivery] = []
        self.max_page_size = 50
        self.seq = 0
        self._tasks: set[asyncio.Task] = set()

    # -- directory --------------------------------------------------------------------

    def add_tenant(self, name: str, domain: str, *, tenant_id: str | None = None) -> MockTenant:
        tenant = MockTenant(tenant_id=tenant_id or _tid(domain), name=name, domain=domain)
        self.tenants[tenant.tenant_id] = tenant
        return tenant

    def add_user(self, tenant: MockTenant, upn: str, display_name: str) -> MockUser:
        user = MockUser(oid=_oid(upn), tenant_id=tenant.tenant_id, upn=upn.lower(), display_name=display_name)
        cal = MockCalendar(id=f"cal-{secrets.token_hex(8)}", owner_oid=user.oid, name="Calendar")
        user.default_calendar_id = cal.id
        self.calendars[cal.id] = cal
        tenant.users[user.oid] = user
        self.users[user.oid] = user
        return user

    def find_tenant(self, ref: str) -> MockTenant | None:
        ref = ref.lower()
        for t in self.tenants.values():
            if t.tenant_id == ref or t.domain == ref:
                return t
        return None

    def find_user(self, ref: str) -> MockUser | None:
        ref = ref.lower()
        if ref in self.users:
            return self.users[ref]
        return next((u for u in self.users.values() if u.upn == ref), None)

    def user_by_upn(self, upn: str) -> MockUser:
        user = self.find_user(upn)
        assert user, upn
        return user

    # -- tokens -----------------------------------------------------------------------

    def _check_client(self, client_id: str, client_secret: str | None) -> None:
        if client_id != self.source_app_id:
            raise MockGraphError(
                400,
                "unauthorized_client",
                f"AADSTS700016: Application with identifier '{client_id}' was not found.",
            )
        if client_secret != self.source_app_secret:
            raise MockGraphError(401, "invalid_client", "AADSTS7000215: Invalid client secret provided.")

    def issue_app_token(self, tenant_ref: str, client_id: str, client_secret: str | None) -> dict[str, Any]:
        """Client credentials: always refused. The only app is delegated-only."""
        tenant = self.find_tenant(tenant_ref)
        if tenant is None:
            raise MockGraphError(400, "invalid_tenant", f"AADSTS90002: Tenant '{tenant_ref}' not found.")
        self._check_client(client_id, client_secret)
        raise MockGraphError(
            400,
            "unauthorized_client",
            f"AADSTS700016: Application '{client_id}' has no application permissions in tenant "
            f"'{tenant.tenant_id}'. The source app is delegated-only: use a user's refresh token.",
        )

    def consent_needed(self, user: MockUser, client_id: str, scopes: list[str]) -> set[str]:
        granted = self.grants.get((user.oid, client_id), set())
        return {s for s in scopes if s not in OIDC_SCOPES} - granted

    def grant(self, user: MockUser, client_id: str, scopes: list[str]) -> None:
        self.grants.setdefault((user.oid, client_id), set()).update(s for s in scopes if s not in OIDC_SCOPES)

    def delegated_tokens(self, user: MockUser, client_id: str, scopes: list[str]) -> dict[str, Any]:
        """Access token (+ rotated refresh token when offline_access was requested)."""
        granted = sorted(self.grants.get((user.oid, client_id), set()))
        body: dict[str, Any] = {
            "token_type": "Bearer",
            "scope": " ".join([*granted, *[s for s in scopes if s in OIDC_SCOPES]]),
            "expires_in": 3599,
            "access_token": self.sign(
                {
                    "kind": "delegated",
                    "tid": user.tenant_id,
                    "oid": user.oid,
                    "appid": client_id,
                    "scp": " ".join(granted),
                    "gen": user.session_generation,
                }
            ),
        }
        if "offline_access" in scopes:
            token = "mrt." + secrets.token_urlsafe(32)
            self.refresh_tokens[token] = MockRefreshToken(
                oid=user.oid,
                tenant_id=user.tenant_id,
                client_id=client_id,
                scopes=scopes,
                generation=user.session_generation,
                expires_at=now_utc() + timedelta(days=REFRESH_TOKEN_DAYS),
            )
            body["refresh_token"] = token
        return body

    def redeem_refresh_token(
        self, tenant_ref: str, client_id: str, client_secret: str | None, token: str, scope: str
    ) -> dict[str, Any]:
        self._check_client(client_id, client_secret)
        grant = self.refresh_tokens.pop(token, None)  # single use: every redemption rotates
        expired = MockGraphError(
            400,
            "invalid_grant",
            "AADSTS700082: The refresh token has expired due to inactivity or was revoked. "
            "The user must sign in again.",
        )
        if grant is None or grant.client_id != client_id or grant.expires_at < now_utc():
            raise expired
        user = self.users.get(grant.oid)
        if user is None or grant.generation != user.session_generation:
            raise expired
        tenant = self.find_tenant(tenant_ref) if tenant_ref not in ("organizations", "common") else None
        if tenant_ref not in ("organizations", "common") and (
            tenant is None or tenant.tenant_id != grant.tenant_id
        ):
            raise MockGraphError(
                400, "invalid_grant", "AADSTS50020: The refresh token was issued for a different tenant."
            )
        requested = [s.rsplit("/", 1)[-1] for s in scope.split()]
        missing = self.consent_needed(user, client_id, requested)
        if missing:
            raise MockGraphError(
                400,
                "invalid_grant",
                f"AADSTS65001: The user or administrator has not consented to use the application "
                f"with ID '{client_id}'. Send an interactive authorization request for this user and "
                f"resource. Missing: {' '.join(sorted(missing))}",
            )
        return self.delegated_tokens(user, client_id, sorted(set(requested) | {"offline_access"}))

    def revoke_sessions(self, user: MockUser) -> int:
        """Like revokeSignInSessions / a password reset: refresh tokens die immediately."""
        user.session_generation += 1
        revoked = [t for t, g in self.refresh_tokens.items() if g.oid == user.oid]
        for token in revoked:
            del self.refresh_tokens[token]
        return len(revoked)

    def withdraw_consent(self, user: MockUser, client_id: str) -> None:
        """The user removed the app at myapps.microsoft.com (delegated grant deleted).
        Refresh tokens then fail with AADSTS65001 (consent required)."""
        self.grants.pop((user.oid, client_id), None)

    def sign(self, claims: dict[str, Any]) -> str:
        import jwt

        now = int(now_utc().timestamp())
        return jwt.encode({"iat": now, "exp": now + 3600, **claims}, self.token_secret, algorithm="HS256")

    def verify(self, token: str) -> dict[str, Any]:
        import jwt

        try:
            claims = jwt.decode(token, self.token_secret, algorithms=["HS256"])
        except jwt.PyJWTError as exc:
            raise MockGraphError(401, "InvalidAuthenticationToken", str(exc)) from exc
        if claims.get("kind") == "delegated":
            user = self.users.get(claims.get("oid", ""))
            if user is None or claims.get("gen") != user.session_generation:
                raise MockGraphError(
                    401,
                    "InvalidAuthenticationToken",
                    "Continuous access evaluation resulted in challenge: the user's sessions were revoked.",
                )
        return claims

    # -- events -----------------------------------------------------------------------

    def _bump(self, cal: MockCalendar, eid: str) -> None:
        self.seq += 1
        cal.changes.append((self.seq, eid))

    def calendar_for(self, user: MockUser, calendar_id: str | None) -> MockCalendar:
        cid = calendar_id or user.default_calendar_id
        cal = self.calendars.get(cid)
        if cal is None or cal.owner_oid != user.oid:
            raise MockGraphError(404, "ErrorItemNotFound", "The specified calendar could not be found.")
        return cal

    def find_event(self, user: MockUser, event_id: str) -> tuple[MockCalendar, dict[str, Any]]:
        for cal in self.calendars.values():
            if cal.owner_oid == user.oid and event_id in cal.events:
                return cal, cal.events[event_id]
        raise MockGraphError(404, "ErrorItemNotFound", "The specified object was not found in the store.")

    def build_event(self, payload: dict[str, Any], *, ical_uid: str | None = None) -> dict[str, Any]:
        all_day = bool(payload.get("isAllDay"))
        start = to_utc(payload["start"], all_day)
        end = to_utc(payload["end"], all_day)
        now = graph_dt(now_utc())
        return {
            "id": "AAMk" + secrets.token_urlsafe(18).replace("-", "x").replace("_", "y"),
            "iCalUId": ical_uid or "040000008200E00074C5B7101A82E008" + secrets.token_hex(24).upper(),
            "type": "singleInstance",
            "seriesMasterId": None,
            "subject": payload.get("subject", ""),
            "start": {"dateTime": graph_dt(start), "timeZone": "UTC"},
            "end": {"dateTime": graph_dt(end), "timeZone": "UTC"},
            "originalStartTimeZone": payload["start"].get("timeZone", "UTC"),
            "originalEndTimeZone": payload["end"].get("timeZone", "UTC"),
            "isAllDay": all_day,
            "showAs": payload.get("showAs", "busy"),
            "sensitivity": payload.get("sensitivity", "normal"),
            "isCancelled": False,
            "isReminderOn": payload.get("isReminderOn", True),
            "location": payload.get("location") or {"displayName": ""},
            "categories": payload.get("categories", []),
            "body": payload.get("body") or {"contentType": "text", "content": ""},
            "attendees": payload.get("attendees", []),
            "createdDateTime": now,
            "lastModifiedDateTime": now,
        }

    def apply_patch(self, event: dict[str, Any], patch: dict[str, Any]) -> None:
        all_day = bool(patch.get("isAllDay", event["isAllDay"]))
        for key in (
            "subject",
            "showAs",
            "sensitivity",
            "location",
            "categories",
            "body",
            "isReminderOn",
            "attendees",
            "isCancelled",
        ):
            if key in patch:
                event[key] = patch[key]
        event["isAllDay"] = all_day
        if "start" in patch:
            event["start"] = {"dateTime": graph_dt(to_utc(patch["start"], all_day)), "timeZone": "UTC"}
            event["originalStartTimeZone"] = patch["start"].get("timeZone", "UTC")
        if "end" in patch:
            event["end"] = {"dateTime": graph_dt(to_utc(patch["end"], all_day)), "timeZone": "UTC"}
            event["originalEndTimeZone"] = patch["end"].get("timeZone", "UTC")
        event["lastModifiedDateTime"] = graph_dt(now_utc())

    async def create_event(
        self,
        user: MockUser,
        payload: dict[str, Any],
        *,
        calendar_id: str | None = None,
        ical_uid: str | None = None,
    ) -> dict[str, Any]:
        cal = self.calendar_for(user, calendar_id)
        event = self.build_event(payload, ical_uid=ical_uid)
        cal.events[event["id"]] = event
        self._bump(cal, event["id"])
        await self.notify(user, cal, event["id"], "created")
        return event

    async def update_event(self, user: MockUser, event_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        cal, event = self.find_event(user, event_id)
        self.apply_patch(event, patch)
        self._bump(cal, event_id)
        await self.notify(user, cal, event_id, "updated")
        return event

    async def delete_event(self, user: MockUser, event_id: str) -> None:
        cal, _ = self.find_event(user, event_id)
        del cal.events[event_id]
        self._bump(cal, event_id)
        await self.notify(user, cal, event_id, "deleted")

    # -- delta ------------------------------------------------------------------------

    def delta(
        self, base_url: str, user: MockUser, calendar_id: str | None, params: dict[str, str], page_size: int
    ):
        page_size = max(1, min(page_size, self.max_page_size))
        if "$skiptoken" in params:
            tok = unb64(params["$skiptoken"])
            ids = self.delta_sessions.get(tok["session"])
            if ids is None:
                raise MockGraphError(410, "SyncStateNotFound", "The sync state generation is not found.")
            return self._delta_page(base_url, user, tok, ids, page_size)
        if "$deltatoken" in params:
            tok = unb64(params["$deltatoken"])
            cal = self.calendars.get(tok["cal"])
            if cal is None or cal.owner_oid != user.oid:
                raise MockGraphError(410, "SyncStateNotFound", "Resync required.")
            changed: list[str] = []
            for seq, eid in cal.changes:
                if seq > tok["seq"] and eid not in changed:
                    changed.append(eid)
            tok = {**tok, "seq_at_start": self.seq, "offset": 0, "session": secrets.token_hex(8)}
            self.delta_sessions[tok["session"]] = changed
            return self._delta_page(base_url, user, tok, changed, page_size)
        start = parse_any(params["startDateTime"])
        end = parse_any(params["endDateTime"])
        cal = self.calendar_for(user, calendar_id)
        ids = sorted(eid for eid, ev in cal.events.items() if self._in_window(ev, start, end))
        tok = {
            "cal": cal.id,
            "start": iso_z(start),
            "end": iso_z(end),
            "seq_at_start": self.seq,
            "offset": 0,
            "session": secrets.token_hex(8),
            "full": True,
        }
        self.delta_sessions[tok["session"]] = ids
        return self._delta_page(base_url, user, tok, ids, page_size)

    def _in_window(self, ev: dict[str, Any], start: datetime, end: datetime) -> bool:
        s = parse_any(ev["start"]["dateTime"])
        e = parse_any(ev["end"]["dateTime"])
        return s < end and e > start and not ev.get("isCancelled")

    def _delta_page(self, base_url: str, user: MockUser, tok: dict, ids: list[str], page_size: int) -> dict:
        cal = self.calendars[tok["cal"]]
        start, end = parse_any(tok["start"]), parse_any(tok["end"])
        chunk = ids[tok["offset"] : tok["offset"] + page_size]
        value = []
        for eid in chunk:
            ev = cal.events.get(eid)
            if ev is not None and self._in_window(ev, start, end):
                value.append(ev)
            elif not tok.get("full"):
                value.append({"id": eid, "@removed": {"reason": "deleted"}})
        link_base = f"{base_url}/v1.0/users/{user.oid}/calendarView/delta"
        page: dict[str, Any] = {"value": value}
        next_offset = tok["offset"] + page_size
        if next_offset < len(ids):
            page["@odata.nextLink"] = f"{link_base}?$skiptoken={b64({**tok, 'offset': next_offset})}"
        else:
            self.delta_sessions.pop(tok["session"], None)
            final = {"cal": cal.id, "start": tok["start"], "end": tok["end"], "seq": tok["seq_at_start"]}
            page["@odata.deltaLink"] = f"{link_base}?$deltatoken={b64(final)}"
        return page

    # -- subscriptions ----------------------------------------------------------------

    def parse_resource(self, resource: str, claims: dict[str, Any]) -> tuple[MockUser, str]:
        tenant_id = claims["tid"]
        parts = [p for p in resource.strip("/").split("/") if p]
        lowered = [p.lower() for p in parts]
        if len(parts) < 3 or lowered[0] != "users" or lowered[-1] != "events":
            raise MockGraphError(400, "InvalidRequest", f"Unsupported resource {resource}")
        user = self.find_user(parts[1])
        if user is None or user.tenant_id != tenant_id:
            raise MockGraphError(404, "ResourceNotFound", "Resource not found for the segment 'users'.")
        if claims.get("kind") == "delegated":
            if user.oid != claims["oid"]:
                raise MockGraphError(
                    403, "ExtensionError", "Operation: Create; Exception: [Access is denied.]"
                )
            if "Calendars.Read" not in claims.get("scp", "").split():
                raise MockGraphError(
                    403, "ExtensionError", "Insufficient privileges: Calendars.Read required."
                )
        if len(parts) == 5 and lowered[2] == "calendars":
            return user, self.calendar_for(user, parts[3]).id
        return user, user.default_calendar_id

    async def validate_endpoint(self, url: str) -> None:
        token = secrets.token_urlsafe(16)
        try:
            resp = await self.notifier.post(url, params={"validationToken": token}, content=b"")
        except httpx.HTTPError as exc:
            raise MockGraphError(
                400, "ValidationError", f"Subscription validation request failed: {exc}"
            ) from exc
        if resp.status_code != 200 or resp.text != token:
            raise MockGraphError(
                400,
                "ValidationError",
                f"Subscription validation request failed. Response must exactly match "
                f"validationToken query parameter (got HTTP {resp.status_code}).",
            )
        self.deliveries.append(Delivery(now_utc(), "validation", url, "ok", ""))

    async def create_subscription(self, claims: dict[str, Any], body: dict[str, Any]) -> MockSubscription:
        expiration = parse_any(body["expirationDateTime"])
        if expiration > now_utc() + timedelta(minutes=MAX_SUBSCRIPTION_MINUTES):
            raise MockGraphError(
                400, "ExtensionError", "Subscription expiration can only be 4230 minutes in the future."
            )
        user, calendar_id = self.parse_resource(body["resource"], claims)
        await self.validate_endpoint(body["notificationUrl"])
        if body.get("lifecycleNotificationUrl"):
            await self.validate_endpoint(body["lifecycleNotificationUrl"])
        sub = MockSubscription(
            id=str(uuid.uuid4()),
            app_id=claims["appid"],
            tenant_id=claims["tid"],
            owner_oid=user.oid,
            calendar_id=calendar_id,
            resource=body["resource"],
            notification_url=body["notificationUrl"],
            lifecycle_url=body.get("lifecycleNotificationUrl"),
            client_state=body.get("clientState"),
            expiration=expiration,
            change_types=set(body.get("changeType", "").split(",")),
        )
        if claims.get("kind") == "delegated":
            sub.creator_oid = claims["oid"]
            sub.creator_generation = claims.get("gen", 0)
        self.subscriptions[sub.id] = sub
        return sub

    def subscription_json(self, sub: MockSubscription) -> dict[str, Any]:
        return {
            "id": sub.id,
            "resource": sub.resource,
            "applicationId": sub.app_id,
            "changeType": ",".join(sorted(sub.change_types)),
            "notificationUrl": sub.notification_url,
            "lifecycleNotificationUrl": sub.lifecycle_url,
            "expirationDateTime": iso_z(sub.expiration),
            "clientState": None,
        }

    # -- notifications ----------------------------------------------------------------

    async def _post(self, kind: str, url: str, payload: dict[str, Any]) -> None:
        try:
            resp = await self.notifier.post(url, json=payload)
            status = str(resp.status_code)
        except httpx.HTTPError as exc:
            status = f"error: {exc.__class__.__name__}"
        self.deliveries.append(Delivery(now_utc(), kind, url, status, json.dumps(payload)[:300]))
        del self.deliveries[:-200]

    async def _deliver(self, kind: str, url: str, payload: dict[str, Any]) -> None:
        if self.inline_delivery:
            await self._post(kind, url, payload)
        else:
            task = asyncio.create_task(self._post(kind, url, payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def notify(self, user: MockUser, cal: MockCalendar, event_id: str, change: str) -> None:
        tenant = self.tenants[user.tenant_id]
        now = now_utc()
        for sub in list(self.subscriptions.values()):
            if sub.owner_oid != user.oid or sub.calendar_id != cal.id or change not in sub.change_types:
                continue
            if sub.expiration < now:
                continue
            if tenant.drop_notifications:
                self.deliveries.append(Delivery(now, "dropped", sub.notification_url, "dropped", event_id))
                continue
            if not self.subscription_authorized(sub):
                self.deliveries.append(
                    Delivery(now, "suspended", sub.notification_url, "grant revoked", event_id)
                )
                continue
            resource = f"Users/{user.oid}/Events/{event_id}"
            await self._deliver(
                f"notification:{change}",
                sub.notification_url,
                {
                    "value": [
                        {
                            "subscriptionId": sub.id,
                            "subscriptionExpirationDateTime": iso_z(sub.expiration),
                            "changeType": change,
                            "resource": resource,
                            "resourceData": {
                                "@odata.type": "#Microsoft.Graph.Event",
                                "@odata.id": resource,
                                "@odata.etag": f'W/"{secrets.token_hex(4)}"',
                                "id": event_id,
                            },
                            "clientState": sub.client_state,
                            "tenantId": sub.tenant_id,
                        }
                    ]
                },
            )

    async def lifecycle(self, sub: MockSubscription, event: str) -> None:
        if not sub.lifecycle_url:
            return
        await self._deliver(
            f"lifecycle:{event}",
            sub.lifecycle_url,
            {
                "value": [
                    {
                        "subscriptionId": sub.id,
                        "subscriptionExpirationDateTime": iso_z(sub.expiration),
                        "tenantId": sub.tenant_id,
                        "clientState": sub.client_state,
                        "lifecycleEvent": event,
                    }
                ]
            },
        )

    def reauthorize(self, sub: MockSubscription, claims: dict[str, Any]) -> None:
        if sub.creator_oid is not None:
            if claims.get("oid") != sub.creator_oid:
                raise MockGraphError(
                    403, "ExtensionError", "Only the subscription's owner can reauthorize it."
                )
            sub.creator_generation = claims.get("gen", 0)

    def subscription_authorized(self, sub: MockSubscription) -> bool:
        if sub.creator_oid is None:
            return True
        creator = self.users.get(sub.creator_oid)
        return (
            creator is not None
            and creator.session_generation == sub.creator_generation
            and "Calendars.Read" in self.grants.get((creator.oid, sub.app_id), set())
        )

    def user_subscriptions(self, user: MockUser) -> list[MockSubscription]:
        return [s for s in self.subscriptions.values() if s.creator_oid == user.oid]

    async def revoke_user(self, user: MockUser, *, withdraw_consent: bool = False) -> int:
        """Revoke a user's refresh tokens (optionally also their consent). Graph asks the
        app to reauthorize the user's subscriptions, which it can no longer do."""
        revoked = 0
        if withdraw_consent:
            self.withdraw_consent(user, self.source_app_id)
        else:
            revoked = self.revoke_sessions(user)
        for sub in self.user_subscriptions(user):
            await self.lifecycle(sub, "reauthorizationRequired")
        return revoked

    def tenant_subscriptions(self, tenant_id: str) -> list[MockSubscription]:
        return [s for s in self.subscriptions.values() if s.tenant_id == tenant_id]

    async def send_lifecycle(self, tenant_id: str, event: str) -> int:
        subs = self.tenant_subscriptions(tenant_id)
        for sub in subs:
            await self.lifecycle(sub, event)
            if event == "subscriptionRemoved":
                self.subscriptions.pop(sub.id, None)
        return len(subs)

    # -- persistence (so a container restart doesn't forget grants and mailboxes) ------

    _PERSISTED = (
        "tenants",
        "users",
        "calendars",
        "subscriptions",
        "seq",
        "deliveries",
        "refresh_tokens",
        "grants",
    )

    def save(self, path: str) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as fh:
            pickle.dump({k: getattr(self, k) for k in self._PERSISTED}, fh)
        os.replace(tmp, path)

    def load(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        with open(path, "rb") as fh:
            data = pickle.load(fh)  # noqa: S301 - our own file on a private volume
        for key in self._PERSISTED:
            if key in data:  # files written by older versions lack the newer keys
                setattr(self, key, data[key])
        return True

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def seed(state: MockState) -> None:
    """Demo directory: three customer tenants, plus ours, which only holds the operator's
    sign-in (it is an ordinary tenant to the app)."""
    ours = state.add_tenant("Calendar Sync", "calsync.example")
    state.add_user(ours, "ops@calsync.example", "Olivia Operator")

    contoso = state.add_tenant("Contoso", "contoso.example")
    fabrikam = state.add_tenant("Fabrikam", "fabrikam.example")
    northwind = state.add_tenant("Northwind Traders", "northwind.example")
    state.add_user(contoso, "alice@contoso.example", "Alice Martin")
    state.add_user(contoso, "bob@contoso.example", "Bob Ito")
    state.add_user(fabrikam, "alice@fabrikam.example", "Alice Martin (Fabrikam board)")
    state.add_user(fabrikam, "carol@fabrikam.example", "Carol Diaz")
    state.add_user(northwind, "alice@northwind.example", "Alice Martin (Northwind)")
    state.add_user(northwind, "dave@northwind.example", "Dave Kim")


async def seed_events(state: MockState) -> None:
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)

    def at(day: int, hour: int, minutes: int = 0) -> dict[str, str]:
        return {
            "dateTime": (today + timedelta(days=day, hours=hour, minutes=minutes)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            "timeZone": "UTC",
        }

    alice_c = state.user_by_upn("alice@contoso.example")
    alice_f = state.user_by_upn("alice@fabrikam.example")
    alice_n = state.user_by_upn("alice@northwind.example")
    await state.create_event(
        alice_c,
        {
            "subject": "Contoso weekly sync",
            "start": at(0, 14),
            "end": at(0, 15),
            "location": {"displayName": "Teams"},
            "attendees": [{"emailAddress": {"address": "bob@contoso.example"}}],
        },
    )
    await state.create_event(
        alice_c, {"subject": "Budget review", "start": at(1, 9), "end": at(1, 10, 30), "showAs": "tentative"}
    )
    await state.create_event(
        alice_f,
        {
            "subject": "Fabrikam board meeting",
            "start": at(1, 10),
            "end": at(1, 12),
            "location": {"displayName": "HQ, Room 4"},
        },
    )
    await state.create_event(
        alice_n,
        {
            "subject": "Northwind offsite",
            "start": at(3, 0),
            "end": at(4, 0),
            "isAllDay": True,
            "showAs": "oof",
        },
    )
    # One meeting that appears in two of Alice's calendars (same iCalUId): shown once.
    shared = await state.create_event(
        alice_c, {"subject": "Contoso x Fabrikam partnership", "start": at(2, 15), "end": at(2, 16)}
    )
    await state.create_event(
        alice_f,
        {"subject": "Contoso x Fabrikam partnership", "start": at(2, 15), "end": at(2, 16)},
        ical_uid=shared["iCalUId"],
    )
