"""Webhook receiver contract, Graph client guards, tokens and session security."""

import asyncio
import uuid

import pytest

from app.graph.client import GraphForbidden, GraphNotFound, GraphThrottled, ReadOnlyViolation
from app.graph.oidc import needs_admin_approval
from app.graph.tokens import TokenError, is_reauth_error
from app.security import (
    InvalidToken,
    issue_oauth_state,
    issue_session,
    read_oauth_state,
    read_session,
    safe_next_path,
)
from tests.harness import SOURCE_APP, Harness, make_settings, tenant_of
from tests.test_sync_flow import link_and_sync

# --- webhook receiver ----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/webhooks/graph", "/webhooks/lifecycle"])
async def test_validation_handshake_echoes_token(harness: Harness, path: str):
    async with harness.client() as client:
        resp = await client.post(f"http://api.test{path}?validationToken=abc%20123%3C")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "abc 123<"


async def _subscribed(h: Harness, operator, alice):
    await link_and_sync(h, alice, "alice@contoso.example")
    return next(iter(h.mock.subscriptions.values()))


def _payload(sub, **overrides):
    item = {
        "subscriptionId": sub.id,
        "changeType": "created",
        "resource": f"Users/{sub.owner_oid}/Events/AAMkX",
        "resourceData": {"id": "AAMkX"},
        "clientState": sub.client_state,
        "tenantId": sub.tenant_id,
    }
    item.update(overrides)
    return {"value": [item]}


async def test_rejects_wrong_client_state(harness: Harness, operator, alice):
    sub = await _subscribed(harness, operator, alice)
    async with harness.client() as client:
        resp = await client.post("http://api.test/webhooks/graph", json=_payload(sub, clientState="forged"))
    assert resp.status_code == 202  # acknowledge so Graph doesn't retry, but drop it
    assert harness.queue.jobs == []


async def test_rejects_missing_client_state_and_unknown_subscription(harness: Harness, operator, alice):
    sub = await _subscribed(harness, operator, alice)
    async with harness.client() as client:
        await client.post("http://api.test/webhooks/graph", json=_payload(sub, clientState=None))
        await client.post(
            "http://api.test/webhooks/graph", json=_payload(sub, subscriptionId=str(uuid.uuid4()))
        )
    assert harness.queue.jobs == []


async def test_rejects_tenant_mismatch(harness: Harness, operator, alice):
    sub = await _subscribed(harness, operator, alice)
    async with harness.client() as client:
        await client.post("http://api.test/webhooks/graph", json=_payload(sub, tenantId=str(uuid.uuid4())))
    assert harness.queue.jobs == []


async def test_valid_notification_is_queued_not_processed(harness: Harness, operator, alice):
    sub = await _subscribed(harness, operator, alice)
    calls_before = len(harness.mock.deliveries)
    async with harness.client() as client:
        resp = await client.post("http://api.test/webhooks/graph", json=_payload(sub))
    assert resp.status_code == 202
    assert harness.queue.jobs == [("process_notification", (sub.id, "AAMkX", "created"), 1)]
    assert len(harness.mock.deliveries) == calls_before


async def test_malformed_body(harness: Harness):
    async with harness.client() as client:
        resp = await client.post(
            "http://api.test/webhooks/graph",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 400


async def test_lifecycle_requires_client_state(harness: Harness, operator, alice):
    sub = await _subscribed(harness, operator, alice)
    body = {
        "value": [
            {
                "subscriptionId": sub.id,
                "lifecycleEvent": "missed",
                "clientState": "forged",
                "tenantId": sub.tenant_id,
            }
        ]
    }
    async with harness.client() as client:
        await client.post("http://api.test/webhooks/lifecycle", json=body)
        body["value"][0]["clientState"] = sub.client_state
        await client.post("http://api.test/webhooks/lifecycle", json=body)
    assert harness.queue.names() == ["handle_lifecycle"]


# --- Graph client --------------------------------------------------------------------


async def test_source_app_cannot_write(harness: Harness, operator, alice):
    await _subscribed(harness, operator, alice)
    tenant_id = tenant_of(harness.mock, "alice@contoso.example")
    source = await harness.source("alice@contoso.example")
    graph = harness.services.graph
    for method in ("POST", "PATCH", "DELETE", "PUT"):
        with pytest.raises(ReadOnlyViolation):
            await graph.request(
                method, "users/x/events/y", source_id=source.id, scope=f"tenant:{tenant_id}", json={}
            )
    # Reads and subscription management are allowed.
    with pytest.raises(GraphNotFound):
        await graph.source(source.id, tenant_id).delete_subscription(str(uuid.uuid4()))


async def test_source_app_cannot_use_client_credentials(harness: Harness):
    contoso = tenant_of(harness.mock, "alice@contoso.example")
    # Our token service has no app-only tokens at all...
    assert not hasattr(harness.services.tokens, "get_token")
    # ...and the identity platform refuses them: the app has no application permissions.
    async with harness.mock_client() as mock:
        for tenant in (contoso, tenant_of(harness.mock, "ops@calsync.example")):
            resp = await mock.post(
                f"/{tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": SOURCE_APP,
                    "client_secret": "source-secret",
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            assert resp.status_code == 400
            assert resp.json()["error"] == "unauthorized_client"


async def test_source_tokens_cached_per_source(harness: Harness, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    await link_and_sync(h, alice, "alice@fabrikam.example")
    contoso = await h.source("alice@contoso.example")
    fabrikam = await h.source("alice@fabrikam.example")
    tokens = h.services.tokens
    a1 = await tokens.get_source_token(contoso.id)
    assert await tokens.get_source_token(contoso.id) == a1
    assert await tokens.get_source_token(fabrikam.id) != a1
    assert await h.services.redis.ttl(f"token:source:{contoso.id}") > 3000


async def test_access_token_refreshes_and_rotation_is_persisted(harness: Harness, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    source = await h.source("alice@contoso.example")
    tokens = h.services.tokens
    first_rt = source.refresh_token
    await tokens.invalidate_source(source.id)

    # Several workers need a token at once: one refresh, everyone gets its result. (The
    # mock rotates strictly, so a second redemption of the same refresh token would fail.)
    results = await asyncio.gather(*(tokens.get_source_token(source.id) for _ in range(5)))
    assert len(set(results)) == 1
    source = await h.source("alice@contoso.example")
    assert source.refresh_token != first_rt
    assert first_rt not in h.mock.refresh_tokens and source.refresh_token in h.mock.refresh_tokens
    assert "Calendars.Read" in source.granted_scopes

    # The refresh went to the source's own tenant endpoint, and the token reads Graph.
    owner = h.mock.user_by_upn("alice@contoso.example")
    claims = h.mock.verify(results[0])
    assert claims["kind"] == "delegated" and claims["oid"] == owner.oid and "Calendars.Read" in claims["scp"]
    graph = h.services.graph.source(source.id, owner.tenant_id)
    assert (await graph.get_default_calendar(owner.oid))["id"] == owner.default_calendar_id

    # A rejected (e.g. expired) access token is dropped and refreshed once, transparently.
    await tokens.cache_source_token(source.id, "garbage", 3600)
    assert (await graph.get_default_calendar(owner.oid))["id"] == owner.default_calendar_id
    assert (await h.source("alice@contoso.example")).refresh_token != source.refresh_token


@pytest.mark.parametrize("scenario,code", [("expired", "AADSTS700082"), ("revoked", "AADSTS700082")])
async def test_dead_refresh_token_requires_reauth(harness: Harness, alice, scenario: str, code: str):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    source = await h.source("alice@contoso.example")
    if scenario == "expired":
        from datetime import timedelta

        from mockgraph.state import now_utc

        h.mock.refresh_tokens[source.refresh_token].expires_at = now_utc() - timedelta(seconds=1)
    else:
        h.mock.revoke_sessions(h.mock.user_by_upn("alice@contoso.example"))
    await h.services.tokens.invalidate_source(source.id)
    with pytest.raises(TokenError) as info:
        await h.services.tokens.get_source_token(source.id)
    assert info.value.reauth_required and info.value.code == code and info.value.source_id == source.id

    # Through a job: the source is paused, not failed, and nothing is retried.
    await h.services.queue.enqueue("sync_source", str(source.id), "scheduled")
    assert await h.run_jobs() == [("sync_source", "reauth-required")]
    assert (await h.source("alice@contoso.example")).status == "reauth_required"
    assert h.queue.deferred == []


async def test_transient_token_failures_retry(harness: Harness, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    source = await h.source("alice@contoso.example")
    await h.services.tokens.invalidate_source(source.id)
    login = h.router.routes.pop("login.test")  # identity platform unreachable
    try:
        await h.services.queue.enqueue("sync_source", str(source.id), "scheduled")
        assert await h.run_jobs() == [("sync_source", "deferred")]
    finally:
        h.router.routes["login.test"] = login
    assert (await h.source("alice@contoso.example")).status == "active"


@pytest.mark.parametrize(
    "error,code,expected",
    [
        ("invalid_grant", "AADSTS700082", True),
        ("invalid_grant", None, True),
        ("interaction_required", "AADSTS50076", True),
        ("invalid_client", "AADSTS50079", True),
        ("consent_required", None, True),
        ("invalid_request", "AADSTS53003", True),
        ("unauthorized_client", "AADSTS7000229", True),
        ("invalid_client", "AADSTS7000215", False),  # our secret is wrong: operator problem
        ("temporarily_unavailable", None, False),
    ],
)
def test_reauth_classification(error, code, expected):
    assert is_reauth_error(error, code) is expected


@pytest.mark.parametrize(
    "error,description,expected",
    [
        ("access_denied", "AADSTS90094: The grant requires admin permission.", True),
        ("access_denied", "AADSTS90008: The user or administrator has not consented.", True),
        ("invalid_client", "AADSTS65001: ... Need admin approval", True),
        ("consent_required", None, True),
        ("access_denied", "AADSTS65004: User declined to consent.", False),
        ("access_denied", "AADSTS65001: The user has not consented", False),
    ],
)
async def test_admin_approval_callback_mapping(harness: Harness, error, description, expected):
    assert needs_admin_approval(error, description) is expected
    params = {"error": error, **({"error_description": description} if description else {})}
    async with harness.client() as client:
        result = harness.result_of(await client.get("/auth/callback", params=params))
    assert result["status"] == ("admin_approval_required" if expected else "error")
    assert result["message"]


async def test_throttle_is_per_tenant(harness: Harness, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    await link_and_sync(h, alice, "alice@fabrikam.example")
    contoso = await h.source("alice@contoso.example")
    fabrikam = await h.source("alice@fabrikam.example")
    contoso_tid = tenant_of(h.mock, "alice@contoso.example")
    fabrikam_tid = tenant_of(h.mock, "alice@fabrikam.example")
    h.mock.tenants[contoso_tid].throttle_remaining = 1
    with pytest.raises(GraphThrottled) as info:
        await h.services.graph.source(contoso.id, contoso_tid).get_default_calendar("alice@contoso.example")
    assert info.value.retry_after == 2
    # Still backing off (without calling Graph), while other tenants are unaffected.
    with pytest.raises(GraphThrottled):
        await h.services.graph.source(contoso.id, contoso_tid).get_default_calendar("alice@contoso.example")
    assert await h.services.graph.source(fabrikam.id, fabrikam_tid).get_default_calendar(
        "alice@fabrikam.example"
    )


async def test_delegated_token_only_reads_its_own_mailbox(harness: Harness, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    source = await h.source("alice@contoso.example")
    graph = h.services.graph.source(source.id, tenant_of(h.mock, "alice@contoso.example"))
    assert await graph.get_default_calendar("alice@contoso.example")
    for other in ("bob@contoso.example", "alice@fabrikam.example"):
        with pytest.raises(GraphForbidden):
            await graph.get_default_calendar(other)


# --- sessions, state, CSRF -----------------------------------------------------------


def test_session_and_state_tokens_are_not_interchangeable():
    settings = make_settings()
    uid = uuid.uuid4()
    session = issue_session(settings, uid)
    assert read_session(settings, session) == uid
    state = issue_oauth_state(settings, purpose="login", nonce="n")
    assert read_oauth_state(settings, state)["nonce"] == "n"
    with pytest.raises(InvalidToken):
        read_session(settings, state)
    with pytest.raises(InvalidToken):
        read_oauth_state(settings, session)
    reauth = issue_oauth_state(settings, purpose="reauth", nonce="n", user_id=uid, source_id=uid)
    assert read_oauth_state(settings, reauth)["sid"] == str(uid)
    with pytest.raises(InvalidToken):
        read_session(settings, reauth)
    with pytest.raises(InvalidToken):
        read_session(make_settings(session_secret="another-secret-" + "y" * 40), session)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/dashboard", "/dashboard"),
        ("//evil.example", "/calendar"),
        ("https://evil.example", "/calendar"),
        ("/\\evil", "/calendar"),
        (None, "/calendar"),
    ],
)
def test_safe_next_path(value, expected):
    assert safe_next_path(value) == expected


async def test_mutations_require_csrf_header(harness: Harness, alice):
    import httpx

    cookies = alice.cookies
    async with httpx.AsyncClient(
        transport=harness.router, base_url="http://web.test", cookies=cookies
    ) as bare:
        assert (await bare.get("/api/me")).status_code == 200
        assert (await bare.delete("/api/me")).status_code == 403


async def test_unauthenticated_requests(harness: Harness):
    async with harness.client() as client:
        assert (await client.get("/api/me")).status_code == 401
        assert (await client.get("/api/admin/overview")).status_code == 401
        assert (await client.get("/auth/link")).status_code == 401
        assert (await client.get(f"/auth/reauth/{uuid.uuid4()}")).status_code == 401


async def test_login_rejects_forged_state(harness: Harness):
    async with harness.client() as client:
        resp = await client.get("/auth/callback", params={"code": "x", "state": "forged"})
        assert resp.status_code in (302, 307)
        assert "status=error" in resp.headers["location"]
        # A valid state without the browser-binding cookie is rejected too.
        state = issue_oauth_state(harness.settings, purpose="login", nonce="n")
        resp = await client.get("/auth/callback", params={"code": "x", "state": state})
        assert "status=error" in resp.headers["location"]


async def test_operator_assigned_from_config(harness: Harness, operator, alice):
    assert (await operator.get("/api/me")).json()["is_operator"] is True
    me = (await alice.get("/api/me")).json()
    assert me == {
        "id": str((await harness.user("alice@contoso.example")).id),
        "email": "alice@contoso.example",
        "display_name": "Alice Martin",
        "is_operator": False,
    }


async def test_sign_in_lands_on_the_calendar(harness: Harness):
    h = harness
    async with h.client() as browser:
        resp = await h._oauth_roundtrip(browser, "/auth/login", "alice@contoso.example")
        assert resp.headers["location"] == "http://web.test/calendar"
        resp = await h._oauth_roundtrip(browser, "/auth/login?next=/dashboard", "alice@contoso.example")
        assert resp.headers["location"] == "http://web.test/dashboard"
    # Signing up doesn't queue anything: there is no calendar to provision.
    assert h.queue.jobs == []
