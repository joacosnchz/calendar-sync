"""HTTP surface of the mock: identity platform, Graph v1.0 subset, and a control panel."""

import html
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mockgraph.state import (
    MockGraphError,
    MockState,
    MockUser,
    now_utc,
    parse_any,
    public_event,
    seed,
    seed_events,
)


def state_from_env() -> MockState:
    return MockState(
        home_tenant_id=os.environ.get("HOME_TENANT_ID", "11111111-1111-4111-8111-111111111111"),
        source_app_id=os.environ["SOURCE_APP_CLIENT_ID"],
        source_app_secret=os.environ["SOURCE_APP_CLIENT_SECRET"],
        primary_app_id=os.environ["PRIMARY_APP_CLIENT_ID"],
        primary_app_secret=os.environ["PRIMARY_APP_CLIENT_SECRET"],
        shared_mailbox=os.environ.get("PRIMARY_SHARED_MAILBOX", "calendars@calsync.example"),
    )


def _graph_error(exc: MockGraphError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status, headers=exc.headers
    )


def create_mock_app(state: MockState | None = None, *, seed_demo: bool = True) -> FastAPI:
    state_file = os.environ.get("MOCKGRAPH_STATE_FILE") if state is None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mock: MockState = app.state.mock
        if state_file and mock.load(state_file):
            pass
        elif seed_demo and not mock.tenants:
            seed(mock)
            await seed_events(mock)
        yield
        if state_file:
            mock.save(state_file)
        await mock.notifier.aclose()

    app = FastAPI(title="Mock Microsoft Graph", lifespan=lifespan)
    app.state.mock = state or state_from_env()

    if state_file:

        @app.middleware("http")
        async def persist(request: Request, call_next):
            response = await call_next(request)
            if request.method != "GET" or request.url.path.endswith("/authorize/complete"):
                request.app.state.mock.save(state_file)
            return response

    def st(request: Request) -> MockState:
        return request.app.state.mock

    def base(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    @app.exception_handler(MockGraphError)
    async def handle(_: Request, exc: MockGraphError) -> JSONResponse:
        return _graph_error(exc)

    # -- identity platform ------------------------------------------------------------

    @app.get("/{tenant}/oauth2/v2.0/authorize", response_class=HTMLResponse)
    async def authorize(request: Request, tenant: str) -> str:
        s = st(request)
        q = dict(request.query_params)
        if tenant not in ("organizations", "common") and s.find_tenant(tenant) is None:
            return _page("Sign in", f"<p>AADSTS90002: Tenant '{html.escape(tenant)}' not found.</p>")
        tenants = [
            t
            for t in s.tenants.values()
            if tenant in ("organizations", "common") or s.find_tenant(tenant) is t
        ]
        hint = (q.get("login_hint") or "").lower()
        accounts = [(t, u) for t in tenants for u in t.users.values() if u.upn != s.shared_mailbox]
        # login_hint: offer that account first, the rest under "Use another account".
        hinted = [(t, u) for t, u in accounts if u.upn == hint]
        others = [(t, u) for t, u in accounts if u.upn != hint]

        def row(t, u) -> str:
            link = "/mock/authorize/complete?" + urlencode({**q, "oid": u.oid, "tenant_path": tenant})
            return (
                f"<a class='account' href='{html.escape(link)}'><b>{html.escape(u.display_name)}</b>"
                f"<span>{html.escape(u.upn)}</span><small>{html.escape(t.name)}</small></a>"
            )

        scopes = html.escape(q.get("scope", ""))
        body = f"<p class='muted'>Mock Microsoft sign-in · requesting <code>{scopes}</code></p>"
        body += "".join(row(t, u) for t, u in hinted)
        if hinted and others:
            body += "<p class='muted'>Use another account</p>"
        body += "".join(row(t, u) for t, u in others)
        return _page("Pick an account", body)

    @app.get("/mock/authorize/complete")
    async def authorize_complete(request: Request) -> Response:
        s = st(request)
        q = dict(request.query_params)
        user = s.find_user(q["oid"])
        if user is None:
            raise MockGraphError(400, "invalid_request", "Unknown user")
        redirect_uri, state = q["redirect_uri"], q.get("state", "")
        tenant_path = q.get("tenant_path", "organizations")
        if tenant_path not in ("organizations", "common"):
            authority = s.find_tenant(tenant_path)
            if authority is None or authority.tenant_id != user.tenant_id:
                params = {
                    "error": "invalid_request",
                    "error_description": f"AADSTS50020: User account '{user.upn}' does not exist in "
                    f"tenant '{tenant_path}'.",
                    "state": state,
                }
                return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)
        scopes = q.get("scope", "").split()
        needed = s.consent_needed(user, q["client_id"], scopes)
        if needed and s.tenants[user.tenant_id].user_consent_blocked:
            params = {
                "error": "access_denied",
                "error_description": "AADSTS90094: The grant requires admin permission. "
                f"{s.tenants[user.tenant_id].name} doesn't allow users to consent to "
                f"{' '.join(sorted(needed))}; an administrator must approve this app (admin approval "
                "required).",
                "state": state,
            }
            return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)
        # The user consents for themselves (incremental consent for the new scopes).
        s.grant(user, q["client_id"], scopes)
        code = secrets.token_urlsafe(24)
        s.codes[code] = {
            "oid": user.oid,
            "nonce": q.get("nonce"),
            "client_id": q["client_id"],
            "redirect_uri": redirect_uri,
            "scopes": scopes,
        }
        params = {"code": code, "state": state, "session_state": secrets.token_hex(8)}
        return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)

    @app.post("/{tenant}/oauth2/v2.0/token")
    async def token(request: Request, tenant: str) -> JSONResponse:
        s = st(request)
        form = {k: str(v) for k, v in (await request.form()).items()}
        client_id, secret = form.get("client_id", ""), form.get("client_secret")
        try:
            grant_type = form.get("grant_type")
            if grant_type == "client_credentials":
                return JSONResponse(s.issue_app_token(tenant, client_id, secret))
            if grant_type == "refresh_token":
                return JSONResponse(
                    s.redeem_refresh_token(
                        tenant, client_id, secret, form.get("refresh_token", ""), form.get("scope", "")
                    )
                )
            if grant_type == "authorization_code":
                s._check_client(client_id, secret)
                grant = s.codes.pop(form.get("code", ""), None)
                if (
                    grant is None
                    or grant["client_id"] != client_id
                    or grant["redirect_uri"] != form.get("redirect_uri")
                ):
                    raise MockGraphError(
                        400, "invalid_grant", "AADSTS70000: The provided authorization code is invalid."
                    )
                user = s.users[grant["oid"]]
                if tenant not in ("organizations", "common") and s.find_tenant(tenant) is not s.tenants.get(
                    user.tenant_id
                ):
                    raise MockGraphError(
                        400, "invalid_grant", "AADSTS50020: The code was issued for a different tenant."
                    )
                claims = {
                    "ver": "2.0",
                    "aud": grant["client_id"],
                    "iss": f"{base(request)}/{user.tenant_id}/v2.0",
                    "tid": user.tenant_id,
                    "oid": user.oid,
                    "sub": user.oid,
                    "preferred_username": user.upn,
                    "email": user.upn,
                    "name": user.display_name,
                    "nonce": grant["nonce"],
                }
                body = s.delegated_tokens(user, client_id, grant["scopes"])
                body["id_token"] = s.sign(claims)
                return JSONResponse(body)
            raise MockGraphError(400, "unsupported_grant_type", "AADSTS70003: unsupported grant type")
        except MockGraphError as exc:
            code = exc.message.split(":")[0].removeprefix("AADSTS")
            return JSONResponse(
                {
                    "error": exc.code,
                    "error_description": exc.message,
                    "error_codes": [int(code)] if code.isdigit() else [],
                },
                status_code=exc.status,
            )

    # -- Graph ------------------------------------------------------------------------

    def graph_auth(request: Request, *, write: bool = False) -> dict[str, Any]:
        s = st(request)
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise MockGraphError(401, "InvalidAuthenticationToken", "Access token is empty.")
        claims = s.verify(header[7:])
        tenant = s.tenants[claims["tid"]]
        if tenant.throttle_remaining > 0:
            tenant.throttle_remaining -= 1
            raise MockGraphError(
                429,
                "TooManyRequests",
                "Application is over its MailboxConcurrency limit.",
                {"Retry-After": "2"},
            )
        if write and claims["appid"] != s.primary_app_id:
            raise MockGraphError(
                403, "ErrorAccessDenied", "Access is denied. Check credentials and try again."
            )
        return claims

    def mailbox(request: Request, claims: dict[str, Any], ref: str) -> MockUser:
        user = st(request).find_user(ref)
        if user is None:
            raise MockGraphError(404, "ErrorInvalidUser", f"The requested user '{ref}' is invalid.")
        if user.tenant_id != claims["tid"]:
            raise MockGraphError(
                403, "ErrorAccessDenied", "Access is denied. Check credentials and try again."
            )
        if claims.get("kind") == "delegated":
            # Delegated Calendars.Read: only the signed-in user's own mailbox.
            if user.oid != claims.get("oid"):
                raise MockGraphError(
                    403, "ErrorAccessDenied", "Access is denied. Check credentials and try again."
                )
            if "Calendars.Read" not in claims.get("scp", "").split():
                raise MockGraphError(
                    403, "ErrorAccessDenied", "Access is denied. The token lacks Calendars.Read."
                )
        return user

    def calendar_json(cal) -> dict[str, Any]:
        return {"id": cal.id, "name": cal.name, "canEdit": True}

    @app.get("/v1.0/users/{uid}/calendar")
    async def get_default_calendar(request: Request, uid: str) -> dict:
        claims = graph_auth(request)
        user = mailbox(request, claims, uid)
        return calendar_json(st(request).calendar_for(user, None))

    @app.get("/v1.0/users/{uid}/calendars/{cid}")
    async def get_calendar(request: Request, uid: str, cid: str) -> dict:
        claims = graph_auth(request)
        return calendar_json(st(request).calendar_for(mailbox(request, claims, uid), cid))

    @app.post("/v1.0/users/{uid}/calendars", status_code=201)
    async def create_calendar(request: Request, uid: str) -> dict:
        from mockgraph.state import MockCalendar

        claims = graph_auth(request, write=True)
        user = mailbox(request, claims, uid)
        body = await request.json()
        cal = MockCalendar(
            id=f"cal-{secrets.token_hex(8)}", owner_oid=user.oid, name=body.get("name", "Calendar")
        )
        st(request).calendars[cal.id] = cal
        return calendar_json(cal)

    @app.delete("/v1.0/users/{uid}/calendars/{cid}", status_code=204)
    async def delete_calendar(request: Request, uid: str, cid: str) -> Response:
        claims = graph_auth(request, write=True)
        cal = st(request).calendar_for(mailbox(request, claims, uid), cid)
        del st(request).calendars[cal.id]
        return Response(status_code=204)

    def page_size(request: Request) -> int:
        prefer = request.headers.get("prefer", "")
        for part in prefer.split(","):
            if "odata.maxpagesize=" in part:
                return int(part.split("=")[1])
        return 50

    @app.get("/v1.0/users/{uid}/calendarView/delta")
    async def delta_default(request: Request, uid: str) -> dict:
        claims = graph_auth(request)
        user = mailbox(request, claims, uid)
        return st(request).delta(base(request), user, None, dict(request.query_params), page_size(request))

    @app.get("/v1.0/users/{uid}/calendars/{cid}/calendarView/delta")
    async def delta_calendar(request: Request, uid: str, cid: str) -> dict:
        claims = graph_auth(request)
        user = mailbox(request, claims, uid)
        return st(request).delta(base(request), user, cid, dict(request.query_params), page_size(request))

    @app.get("/v1.0/users/{uid}/events/{eid}")
    async def get_event(request: Request, uid: str, eid: str) -> dict:
        claims = graph_auth(request)
        _, event = st(request).find_event(mailbox(request, claims, uid), eid)
        return public_event(event)

    @app.post("/v1.0/users/{uid}/calendars/{cid}/events", status_code=201)
    async def create_event_in(request: Request, uid: str, cid: str) -> dict:
        claims = graph_auth(request, write=True)
        user = mailbox(request, claims, uid)
        return public_event(await st(request).create_event(user, await request.json(), calendar_id=cid))

    @app.post("/v1.0/users/{uid}/events", status_code=201)
    async def create_event_default(request: Request, uid: str) -> dict:
        claims = graph_auth(request, write=True)
        user = mailbox(request, claims, uid)
        return public_event(await st(request).create_event(user, await request.json()))

    @app.patch("/v1.0/users/{uid}/events/{eid}")
    async def patch_event(request: Request, uid: str, eid: str) -> dict:
        claims = graph_auth(request, write=True)
        user = mailbox(request, claims, uid)
        return public_event(await st(request).update_event(user, eid, await request.json()))

    @app.delete("/v1.0/users/{uid}/events/{eid}", status_code=204)
    async def remove_event(request: Request, uid: str, eid: str) -> Response:
        claims = graph_auth(request, write=True)
        await st(request).delete_event(mailbox(request, claims, uid), eid)
        return Response(status_code=204)

    @app.post("/v1.0/subscriptions", status_code=201)
    async def create_subscription(request: Request) -> dict:
        claims = graph_auth(request)
        sub = await st(request).create_subscription(claims, await request.json())
        return st(request).subscription_json(sub)

    def owned_subscription(request: Request, claims: dict[str, Any], sid: str):
        sub = st(request).subscriptions.get(sid)
        if sub is None or sub.app_id != claims["appid"] or sub.tenant_id != claims["tid"]:
            raise MockGraphError(404, "ResourceNotFound", f"The object was not found: {sid}")
        if sub.creator_oid is not None and claims.get("oid") != sub.creator_oid:
            raise MockGraphError(403, "ExtensionError", "Operation: Access denied for this subscription.")
        return sub

    @app.patch("/v1.0/subscriptions/{sid}")
    async def renew_subscription(request: Request, sid: str) -> dict:
        claims = graph_auth(request)
        sub = owned_subscription(request, claims, sid)
        expiration = parse_any((await request.json())["expirationDateTime"])
        if expiration > now_utc() + timedelta(minutes=4230):
            raise MockGraphError(
                400, "ExtensionError", "Subscription expiration can only be 4230 minutes in the future."
            )
        sub.expiration = expiration
        return st(request).subscription_json(sub)

    @app.post("/v1.0/subscriptions/{sid}/reauthorize", status_code=204)
    async def reauthorize(request: Request, sid: str) -> Response:
        claims = graph_auth(request)
        st(request).reauthorize(owned_subscription(request, claims, sid), claims)
        return Response(status_code=204)

    @app.delete("/v1.0/subscriptions/{sid}", status_code=204)
    async def delete_subscription(request: Request, sid: str) -> Response:
        owned_subscription(request, graph_auth(request), sid)
        st(request).subscriptions.pop(sid, None)
        return Response(status_code=204)

    # -- control panel ----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def control_panel(request: Request) -> str:
        return render_panel(st(request))

    @app.post("/mock/events")
    async def ui_create_event(
        request: Request,
        mailbox_upn: str = Form(...),
        subject: str = Form(...),
        date: str = Form(...),
        time: str = Form("09:00"),
        minutes: int = Form(60),
        tz: str = Form("UTC"),
        show_as: str = Form("busy"),
        location: str = Form(""),
        all_day: str | None = Form(None),
    ) -> Response:
        s = st(request)
        start = parse_any(f"{date}T{time}:00").replace(tzinfo=None)
        if all_day:
            start = start.replace(hour=0, minute=0)
            end = start + timedelta(days=1)
        else:
            end = start + timedelta(minutes=minutes)
        await s.create_event(
            s.user_by_upn(mailbox_upn),
            {
                "subject": subject,
                "start": {"dateTime": start.isoformat(), "timeZone": tz},
                "end": {"dateTime": end.isoformat(), "timeZone": tz},
                "isAllDay": bool(all_day),
                "showAs": show_as,
                "location": {"displayName": location},
            },
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/mock/events/{oid}/{eid}")
    async def ui_event_action(
        request: Request, oid: str, eid: str, action: str = Form(...), target: str = Form("")
    ) -> Response:
        s = st(request)
        user = s.users[oid]
        _, event = s.find_event(user, eid)
        if action == "delete":
            await s.delete_event(user, eid)
        elif action == "cancel":
            await s.update_event(user, eid, {"isCancelled": True, "subject": "Canceled: " + event["subject"]})
        elif action == "later":
            start = parse_any(event["start"]["dateTime"]) + timedelta(hours=1)
            end = parse_any(event["end"]["dateTime"]) + timedelta(hours=1)
            await s.update_event(
                user,
                eid,
                {
                    "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
                    "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
                },
            )
        elif action == "rename":
            await s.update_event(user, eid, {"subject": event["subject"] + " (updated)"})
        elif action == "tentative":
            await s.update_event(
                user, eid, {"showAs": "free" if event["showAs"] == "tentative" else "tentative"}
            )
        elif action == "invite" and target:
            # Same meeting in another mailbox, as an invitation would produce: same iCalUId.
            await s.create_event(
                s.users[target],
                {k: event[k] for k in ("subject", "start", "end", "location", "isAllDay", "showAs")},
                ical_uid=event["iCalUId"],
            )
        return RedirectResponse("/", status_code=303)

    @app.post("/mock/tenants/{tid}")
    async def ui_tenant_action(request: Request, tid: str, action: str = Form(...)) -> Response:
        s = st(request)
        tenant = s.tenants[tid]
        if action == "consent_block":
            tenant.user_consent_blocked = not tenant.user_consent_blocked
        elif action == "throttle":
            tenant.throttle_remaining += 5
        elif action == "drop":
            tenant.drop_notifications = not tenant.drop_notifications
        elif action in ("missed", "subscriptionRemoved", "reauthorizationRequired"):
            await s.send_lifecycle(tid, action)
        return RedirectResponse("/", status_code=303)

    @app.post("/mock/users/{ref}/revoke")
    async def ui_revoke_user(request: Request, ref: str, consent: str | None = Form(None)) -> Response:
        """Revoke a user's refresh tokens (like revokeSignInSessions or a password reset).
        With consent=1, delete their consent to the source app instead."""
        s = st(request)
        user = s.find_user(ref)
        if user is None:
            raise MockGraphError(404, "ErrorInvalidUser", f"Unknown user {ref}")
        await s.revoke_user(user, withdraw_consent=bool(consent))
        return RedirectResponse("/", status_code=303)

    @app.get("/mock/state")
    async def dump_state(request: Request) -> dict:
        s = st(request)
        return {
            "tenants": {
                t.tenant_id: {"name": t.name, "user_consent_blocked": t.user_consent_blocked}
                for t in s.tenants.values()
            },
            "grants": {s.users[oid].upn: sorted(scopes) for (oid, _), scopes in s.grants.items()},
            "subscriptions": [s.subscription_json(x) for x in s.subscriptions.values()],
            "calendars": {
                c.id: {
                    "owner": s.users[c.owner_oid].upn,
                    "owner_oid": c.owner_oid,
                    "name": c.name,
                    "events": list(c.events.values()),
                }
                for c in s.calendars.values()
            },
        }

    return app


# --- control panel rendering ---------------------------------------------------------

_CSS = """
body{font:14px/1.45 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2433}
main{max-width:1180px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}h3{font-size:14px;margin:0 0 6px}
small,.muted{color:#6b7385}.card{background:#fff;border:1px solid #e2e5ea;border-radius:10px;padding:14px;margin:10px 0}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:5px 6px;border-top:1px solid #eef0f3;
vertical-align:top}th{color:#6b7385;font-weight:500;border-top:0}button{font:inherit;font-size:12px;padding:3px 8px;
border-radius:6px;border:1px solid #2f6feb;background:#2f6feb;color:#fff;cursor:pointer}
button.secondary{background:#fff;color:#2f6feb}form.inline{display:inline}
input,select{font:inherit;font-size:13px;padding:4px 6px;border:1px solid #cdd2da;border-radius:6px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:#eef0f3}
.ok{background:#dcf5e3;color:#18663a}.bad{background:#fde2e1;color:#9b1c1c}.warn{background:#fff3cd;color:#7a5b00}
.account{display:block;background:#fff;border:1px solid #e2e5ea;border-radius:10px;padding:12px;margin:8px 0;
text-decoration:none;color:inherit}.account span{display:block}.grid{display:grid;grid-template-columns:repeat(auto-fill,
minmax(160px,1fr));gap:8px;align-items:end}code{font-size:12px}
"""


def _page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main><h1>{html.escape(title)}</h1>"
        f"{body}</main></body></html>"
    )


def _fmt(ev: dict) -> str:
    s, e = parse_any(ev["start"]["dateTime"]), parse_any(ev["end"]["dateTime"])
    if ev.get("isAllDay"):
        return f"{s:%a %d %b} (all day)"
    return f"{s:%a %d %b %H:%M}–{e:%H:%M} UTC"


def render_panel(s: MockState) -> str:
    esc = html.escape
    out = [
        "<p class='muted'>Stand-in for login.microsoftonline.com and graph.microsoft.com. "
        "Changes here fire change notifications to subscribed webhooks, like real Graph.</p>"
    ]
    users = [u for t in s.tenants.values() if not t.is_home for u in t.users.values()]
    options = "".join(f"<option value='{esc(u.upn)}'>{esc(u.upn)}</option>" for u in users)
    tomorrow = (now_utc() + timedelta(days=1)).strftime("%Y-%m-%d")
    out.append(
        "<div class='card'><h3>Create an event in a source mailbox</h3><form method='post' action='/mock/events' "
        f"class='grid'><label>Mailbox<br><select name='mailbox_upn'>{options}</select></label>"
        "<label>Subject<br><input name='subject' value='New meeting' required></label>"
        f"<label>Date<br><input type='date' name='date' value='{tomorrow}'></label>"
        "<label>Start<br><input type='time' name='time' value='11:00'></label>"
        "<label>Minutes<br><input type='number' name='minutes' value='60' min='5'></label>"
        "<label>Time zone<br><select name='tz'><option>UTC</option><option>Europe/London</option>"
        "<option>Pacific Standard Time</option><option>W. Europe Standard Time</option>"
        "<option>America/New_York</option></select></label>"
        "<label>Show as<br><select name='show_as'><option>busy</option><option>tentative</option><option>free</option>"
        "<option>oof</option><option>workingElsewhere</option></select></label>"
        "<label>Location<br><input name='location'></label><label><input type='checkbox' name='all_day'> All day</label>"
        "<button>Create</button></form></div>"
    )

    for t in s.tenants.values():
        badge = (
            "<span class='pill bad'>user consent blocked</span>"
            if t.user_consent_blocked
            else "<span class='pill ok'>users can consent</span>"
        )
        if t.is_home:
            badge = "<span class='pill'>host tenant · primary calendars</span>"
        flags = []
        if t.drop_notifications:
            flags.append("<span class='pill bad'>dropping notifications</span>")
        if t.throttle_remaining:
            flags.append(f"<span class='pill warn'>throttling next {t.throttle_remaining}</span>")
        out.append(
            f"<h2>{esc(t.name)} <small>{esc(t.domain)} · <code>{t.tenant_id}</code></small></h2>"
            f"<div>{badge} {' '.join(flags)}</div>"
        )
        if not t.is_home:
            buttons = [
                ("consent_block", "Toggle blocking user consent"),
                ("throttle", "Throttle next 5 calls"),
                ("drop", "Toggle dropping notifications"),
                ("missed", "Send 'missed'"),
                ("subscriptionRemoved", "Send 'subscriptionRemoved'"),
                ("reauthorizationRequired", "Send 'reauthorizationRequired'"),
            ]
            out.append(
                "<div style='margin:8px 0'>"
                + " ".join(
                    f"<form class='inline' method='post' action='/mock/tenants/{t.tenant_id}'>"
                    f"<button class='secondary' name='action' value='{a}'>{label}</button></form>"
                    for a, label in buttons
                )
                + "</div>"
            )
        for u in t.users.values():
            cals = [c for c in s.calendars.values() if c.owner_oid == u.oid]
            for cal in cals:
                events = sorted(cal.events.values(), key=lambda e: e["start"]["dateTime"])
                if t.is_home and u.upn != s.shared_mailbox and not events:
                    continue
                title = f"{esc(u.display_name)} <small>{esc(u.upn)} · {esc(cal.name)}</small>"
                if not t.is_home:
                    scopes = sorted(s.grants.get((u.oid, s.source_app_id), set()))
                    tokens = sum(1 for g in s.refresh_tokens.values() if g.oid == u.oid)
                    title += (
                        f" <small>· granted: {esc(' '.join(scopes)) or 'nothing'} · {tokens} refresh token(s)"
                        "</small>"
                        f"<form class='inline' method='post' action='/mock/users/{u.oid}/revoke'> "
                        "<button class='secondary'>Revoke refresh tokens</button></form>"
                        f"<form class='inline' method='post' action='/mock/users/{u.oid}/revoke'>"
                        "<input type='hidden' name='consent' value='1'> "
                        "<button class='secondary'>Withdraw consent</button></form>"
                    )
                rows = []
                for ev in events:
                    actions = ""
                    if not t.is_home:
                        invite = "".join(
                            f"<option value='{o.oid}'>{esc(o.upn)}</option>" for o in users if o.oid != u.oid
                        )
                        actions = (
                            f"<form class='inline' method='post' action='/mock/events/{u.oid}/{ev['id']}'>"
                            "<button class='secondary' name='action' value='later'>+1h</button> "
                            "<button class='secondary' name='action' value='rename'>Rename</button> "
                            "<button class='secondary' name='action' value='tentative'>Tentative⇄Free</button> "
                            "<button class='secondary' name='action' value='cancel'>Cancel</button> "
                            "<button class='secondary' name='action' value='delete'>Delete</button> "
                            f"<select name='target'>{invite}</select> "
                            "<button class='secondary' name='action' value='invite'>Invite</button></form>"
                        )
                    cancelled = " <span class='pill bad'>cancelled</span>" if ev.get("isCancelled") else ""
                    rows.append(
                        f"<tr><td>{esc(ev['subject'])}{cancelled}<br><small>{esc(ev['location'].get('displayName', ''))}"
                        f"</small></td><td>{_fmt(ev)}</td><td>{esc(ev['showAs'])}</td>"
                        f"<td>{len(ev.get('attendees') or [])}</td><td><code>…{ev['iCalUId'][-6:]}</code></td>"
                        f"<td>{actions}</td></tr>"
                    )
                table = (
                    (
                        "<table><tr><th>Subject</th><th>When</th><th>Show as</th><th>Attendees</th><th>iCalUId</th>"
                        "<th></th></tr>" + "".join(rows) + "</table>"
                    )
                    if rows
                    else "<p class='muted'>No events</p>"
                )
                out.append(f"<div class='card'><h3>{title}</h3>{table}</div>")

    subs = "".join(
        f"<tr><td><code>{x.id[:8]}</code></td><td>{esc(x.resource)}</td><td>{x.expiration:%d %b %H:%M} UTC</td>"
        f"<td>{esc(x.notification_url)}</td></tr>"
        for x in s.subscriptions.values()
    )
    out.append(
        "<h2>Subscriptions</h2><div class='card'><table><tr><th>Id</th><th>Resource</th><th>Expires</th>"
        f"<th>Notification URL</th></tr>{subs}</table></div>"
    )
    log_rows = "".join(
        f"<tr><td>{d.at:%H:%M:%S}</td><td>{esc(d.kind)}</td><td>{esc(d.status)}</td><td><small>{esc(d.url)}</small>"
        f"</td></tr>"
        for d in reversed(s.deliveries[-30:])
    )
    out.append(
        "<h2>Recent deliveries</h2><div class='card'><table><tr><th>Time</th><th>Kind</th><th>Status</th>"
        f"<th>URL</th></tr>{log_rows}</table></div>"
    )
    return _page("Mock Microsoft 365", "".join(out))


app = create_mock_app() if os.environ.get("SOURCE_APP_CLIENT_ID") else None
