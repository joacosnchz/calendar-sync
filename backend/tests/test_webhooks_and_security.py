"""Webhook receiver contract, Graph client guards, tokens and session security."""

import uuid

import pytest

from app.graph.client import GraphNotFound, GraphThrottled, ReadOnlyViolation
from app.graph.tokens import App, TokenError
from app.security import (
    InvalidToken,
    issue_consent_state,
    issue_oauth_state,
    issue_session,
    read_oauth_state,
    read_session,
    safe_next_path,
)
from tests.harness import HOME_TENANT, SOURCE_APP, Harness, make_settings, tenant_of
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
    await h.onboard_tenant(operator, "contoso.example")
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
    graph = harness.services.graph
    for method in ("POST", "PATCH", "DELETE", "PUT"):
        with pytest.raises(ReadOnlyViolation):
            await graph.request(
                method,
                "users/x/events/y",
                app=App.SOURCE,
                tenant_id=tenant_id,
                scope=f"tenant:{tenant_id}",
                json={},
            )
    # Reads and subscription management are allowed.
    with pytest.raises(GraphNotFound):
        await graph.source(tenant_id).delete_subscription(str(uuid.uuid4()))


async def test_primary_app_only_in_home_tenant(harness: Harness):
    with pytest.raises(TokenError):
        await harness.services.tokens.get_token(App.PRIMARY, tenant_of(harness.mock, "alice@contoso.example"))
    assert await harness.services.tokens.get_token(App.PRIMARY, HOME_TENANT)


async def test_tokens_cached_per_tenant(harness: Harness):
    tokens = harness.services.tokens
    contoso = tenant_of(harness.mock, "alice@contoso.example")
    fabrikam = tenant_of(harness.mock, "alice@fabrikam.example")
    for tid in (contoso, fabrikam):
        harness.mock.tenants[tid].consented_apps.add(SOURCE_APP)
    a1 = await tokens.get_token(App.SOURCE, contoso)
    assert await tokens.get_token(App.SOURCE, contoso) == a1
    assert await tokens.get_token(App.SOURCE, fabrikam) != a1
    assert await harness.services.redis.ttl(f"token:source:{contoso}") > 3000


async def test_unconsented_tenant_token_is_revocation(harness: Harness):
    with pytest.raises(TokenError) as info:
        await harness.services.tokens.get_token(App.SOURCE, tenant_of(harness.mock, "alice@contoso.example"))
    assert info.value.revoked and info.value.code == "AADSTS7000229"


async def test_throttle_is_per_tenant(harness: Harness):
    contoso = tenant_of(harness.mock, "alice@contoso.example")
    fabrikam = tenant_of(harness.mock, "alice@fabrikam.example")
    for tid in (contoso, fabrikam):
        harness.mock.tenants[tid].consented_apps.add(SOURCE_APP)
    harness.mock.tenants[contoso].throttle_remaining = 1
    with pytest.raises(GraphThrottled) as info:
        await harness.services.graph.source(contoso).get_default_calendar("alice@contoso.example")
    assert info.value.retry_after == 2
    # Still backing off (without calling Graph), while other tenants are unaffected.
    with pytest.raises(GraphThrottled):
        await harness.services.graph.source(contoso).get_default_calendar("alice@contoso.example")
    assert await harness.services.graph.source(fabrikam).get_default_calendar("alice@fabrikam.example")


async def test_cross_tenant_mailbox_access_is_denied(harness: Harness):
    from app.graph.client import GraphForbidden

    contoso = tenant_of(harness.mock, "alice@contoso.example")
    harness.mock.tenants[contoso].consented_apps.add(SOURCE_APP)
    with pytest.raises(GraphForbidden):
        await harness.services.graph.source(contoso).get_default_calendar("alice@fabrikam.example")


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
    with pytest.raises(InvalidToken):
        read_session(settings, issue_consent_state(settings, uid))
    with pytest.raises(InvalidToken):
        read_session(make_settings(session_secret="another-secret-" + "y" * 40), session)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/calendar", "/calendar"),
        ("//evil.example", "/dashboard"),
        ("https://evil.example", "/dashboard"),
        ("/\\evil", "/dashboard"),
        (None, "/dashboard"),
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
    assert (await alice.get("/api/me")).json()["is_operator"] is False
