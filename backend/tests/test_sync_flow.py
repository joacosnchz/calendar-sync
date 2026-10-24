"""End-to-end flows through API, queue, workers and mock Graph."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from prometheus_client import REGISTRY
from sqlalchemy import select, text, update

from app.models import LinkedSource, SourceEvent, SourceStatus, Subscription, Tenant, User
from app.timeutil import utcnow
from app.worker import sweeps
from mockgraph.state import b64
from tests.harness import Harness, tenant_of


async def link_and_sync(h: Harness, browser, upn: str) -> dict:
    result = await h.link(browser, upn)
    await h.run_jobs()
    return result


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def test_link_stores_refresh_token_and_backfills_without_admin_consent(harness: Harness, alice):
    h = harness
    await h.create_source_event(
        "alice@contoso.example",
        "Existing meeting",
        location={"displayName": "Room 7"},
        attendees=[{"emailAddress": {"address": "bob@contoso.example"}}],
        body={"contentType": "text", "content": "Agenda"},
    )

    # Alice consents for her own mailbox: no tenant onboarding, no admin involved.
    start = await alice.get("/auth/link")
    assert "/organizations/oauth2/v2.0/authorize" in start.headers["location"]
    assert "Calendars.Read" in start.headers["location"] and "offline_access" in start.headers["location"]
    result = await h.link(alice, "alice@contoso.example")
    assert result == {"status": "linked", "email": "alice@contoso.example"}
    owner = h.mock.user_by_upn("alice@contoso.example")
    assert "Calendars.Read" in h.mock.grants[(owner.oid, h.settings.source_app_client_id)]

    source = await h.source("alice@contoso.example")
    assert source.status == SourceStatus.ACTIVE
    assert source.refresh_token and source.refresh_token in h.mock.refresh_tokens
    assert "Calendars.Read" in (source.granted_scopes or "")
    async with h.services.sessionmaker() as session:
        raw = await session.scalar(
            text("SELECT refresh_token FROM linked_sources WHERE id = :id"), {"id": source.id}
        )
        assert raw != source.refresh_token  # encrypted at rest
        tenant = await session.scalar(select(Tenant))
        assert tenant.tenant_id == owner.tenant_id and tenant.domain == "contoso.example"

    assert h.queue.names() == ["setup_source"]
    await h.run_jobs()
    assert len(h.mock.subscriptions) == 1
    sub = next(iter(h.mock.subscriptions.values()))
    assert sub.resource == f"users/{owner.oid}/events"
    assert sub.creator_oid == owner.oid  # created with Alice's delegated token
    assert sub.change_types == {"created", "updated", "deleted"}
    [event] = await h.calendar(alice)
    assert event["subject"] == "Existing meeting" and event["location"] == "Room 7"
    assert event["labels"] == ["Contoso"] and event["source_ids"] == [str(source.id)]
    assert set(event) == {
        "key",
        "subject",
        "location",
        "start",
        "end",
        "is_all_day",
        "show_as",
        "labels",
        "source_ids",
    }
    # FR-9: attendees and body are never stored.
    assert not {"attendees", "body"} & set(SourceEvent.__table__.columns.keys())

    [out] = (await alice.get("/api/sources")).json()
    assert out["status"] == "active"
    assert out["reauth_url"] is None and out["reauth_required_at"] is None
    assert "consent_url" not in out and "consent_status" not in out["tenant"]
    assert out["subscription"]["status"] == "active"
    assert out["event_count"] == 1
    assert out["last_full_sync_at"] is not None


async def test_notifications_create_update_delete(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    upn = "alice@contoso.example"
    owner = h.mock.user_by_upn(upn)

    created = await h.create_source_event(upn, "Kickoff", showAs="tentative")
    assert h.queue.names() == ["process_notification"]
    assert await h.run_jobs() == [("process_notification", "changed=1 delta=False")]
    [event] = await h.calendar(alice)
    assert event["subject"] == "Kickoff" and event["show_as"] == "tentative"

    moved_start = (utcnow() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    await h.mock.update_event(
        owner,
        created["id"],
        {
            "subject": "Kickoff (moved)",
            "start": {"dateTime": moved_start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            "end": {
                "dateTime": (moved_start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "UTC",
            },
        },
    )
    await h.run_jobs()
    [event] = await h.calendar(alice)
    assert event["subject"] == "Kickoff (moved)"
    assert at(event["start"]) == moved_start and at(event["end"]) == moved_start + timedelta(hours=1)

    await h.mock.update_event(owner, created["id"], {"isCancelled": True})
    await h.run_jobs()
    assert await h.calendar(alice) == []

    second = await h.create_source_event(upn, "Standup")
    await h.run_jobs()
    assert await h.subjects(alice) == ["Standup"]
    await h.mock.delete_event(owner, second["id"])
    await h.run_jobs()
    assert await h.calendar(alice) == []
    assert await h.source_event_count() == 0


async def test_duplicate_and_stale_notifications_are_harmless(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    event = await h.create_source_event("alice@contoso.example", "Once")
    sub = next(iter(h.mock.subscriptions.values()))
    # Graph may deliver the same notification many times; queued duplicates collapse.
    for _ in range(5):
        await sub_notify(h, sub, event["id"])
    assert h.queue.names().count("process_notification") == 1
    await h.run_jobs()
    # Re-delivery after processing re-fetches but writes nothing new.
    await sub_notify(h, sub, event["id"])
    assert await h.run_jobs() == [("process_notification", "changed=0 delta=False")]
    assert await h.subjects(alice) == ["Once"]
    assert await h.source_event_count() == 1


async def sub_notify(h: Harness, sub, event_id: str) -> None:
    async with h.client() as client:
        resp = await client.post(
            "http://api.test/webhooks/graph",
            json={
                "value": [
                    {
                        "subscriptionId": sub.id,
                        "changeType": "updated",
                        "resource": f"Users/{sub.owner_oid}/Events/{event_id}",
                        "resourceData": {"id": event_id},
                        "clientState": sub.client_state,
                        "tenantId": sub.tenant_id,
                    }
                ]
            },
        )
    assert resp.status_code == 202


async def test_same_meeting_in_two_sources_shows_once(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    await link_and_sync(h, alice, "alice@fabrikam.example")

    original = await h.create_source_event("alice@contoso.example", "Partnership", showAs="tentative")
    await h.create_source_event("alice@fabrikam.example", "Partnership", ical_uid=original["iCalUId"])
    await h.run_jobs()
    assert await h.source_event_count() == 2
    sources = {s["source_email"]: s for s in (await alice.get("/api/sources")).json()}
    # FR-14: one entry for both copies; FR-10: the busiest show-as wins.
    [event] = await h.calendar(alice)
    assert event["subject"] == "Partnership" and event["key"] == f"ical:{original['iCalUId']}"
    assert event["labels"] == ["Contoso", "Fabrikam"] and event["show_as"] == "busy"
    assert sorted(event["source_ids"]) == sorted(s["id"] for s in sources.values())

    # Unlinking one source keeps the meeting (still in the other) and relabels it.
    resp = await alice.delete(f"/api/sources/{sources['alice@fabrikam.example']['id']}")
    assert resp.status_code == 202
    await h.run_jobs()
    [event] = await h.calendar(alice)
    assert event["labels"] == ["Contoso"] and event["show_as"] == "tentative"
    assert event["source_ids"] == [sources["alice@contoso.example"]["id"]]
    assert len(h.mock.subscriptions) == 1
    assert [s["source_email"] for s in (await alice.get("/api/sources")).json()] == ["alice@contoso.example"]


async def test_unlink_removes_the_sources_events_and_data(harness: Harness, operator, alice):
    h = harness
    await h.create_source_event("alice@contoso.example", "A")
    await h.create_source_event("alice@contoso.example", "B", hours_from_now=48)
    await link_and_sync(h, alice, "alice@contoso.example")
    assert await h.subjects(alice) == ["A", "B"]

    [source] = (await alice.get("/api/sources")).json()
    await alice.delete(f"/api/sources/{source['id']}")
    # Hidden from the calendar as soon as the unlink is requested (status "removing").
    assert await h.calendar(alice) == []
    assert await h.run_jobs() == [("remove_source", "purged events=2")]
    assert h.mock.subscriptions == {}  # deleted remotely with Alice's token
    assert await h.source_event_count() == 0
    async with h.services.sessionmaker() as session:
        assert (await session.scalars(select(LinkedSource.id))).all() == []
    assert not await h.services.redis.exists(f"token:source:{source['id']}")


async def _paused_by_revocation(h: Harness, alice) -> LinkedSource:
    """Alice's Contoso source, synced with one event, then her refresh tokens revoked."""
    await h.create_source_event("alice@contoso.example", "Board prep")
    await link_and_sync(h, alice, "alice@contoso.example")
    assert await h.subjects(alice) == ["Board prep"]
    # Revoked sessions (password reset, admin revocation...): Graph asks us to
    # reauthorize the delegated subscription, and refreshing the token fails.
    await h.mock.revoke_user(h.mock.user_by_upn("alice@contoso.example"))
    ran = await h.run_jobs()
    assert ("handle_lifecycle", "reauth-required") in ran
    return await h.source("alice@contoso.example")


async def test_revoked_refresh_token_marks_reauth_required_and_keeps_events(harness: Harness, alice):
    h = harness
    source = await _paused_by_revocation(h, alice)
    assert source.status == SourceStatus.REAUTH_REQUIRED
    assert source.reauth_required_at is not None
    assert "AADSTS700082" in source.last_error
    # Nothing is deleted: stored events (still in the calendar) and the subscription row stay.
    assert await h.subjects(alice) == ["Board prep"]
    assert await h.source_event_count() == 1
    assert len(await h.subscription_ids()) == 1
    assert not await h.services.redis.exists(f"token:source:{source.id}")

    [out] = (await alice.get("/api/sources")).json()
    assert out["status"] == "reauth_required"
    assert out["reauth_url"] == f"/auth/reauth/{source.id}"
    assert out["reauth_required_at"] is not None and "AADSTS700082" in out["last_error"]

    async with h.services.sessionmaker() as session:
        from app.models import AuditLog, SyncError

        assert await session.scalar(select(AuditLog).where(AuditLog.action == "source.reauth_required"))
        errors = (await session.scalars(select(SyncError.kind))).all()
        assert errors == ["source.reauth_required"]

    # Paused sources are skipped by every sweep and their events stay visible.
    async with h.services.sessionmaker() as session:
        await session.execute(
            update(LinkedSource).values(
                last_synced_at=utcnow() - timedelta(hours=7), created_at=utcnow() - timedelta(hours=1)
            )
        )
        await session.execute(update(Subscription).values(renew_due_at=utcnow() - timedelta(minutes=1)))
        await session.commit()
    ctx = {"services": h.services}
    assert await sweeps.delta_sweep(ctx) == "queued=0"
    assert await sweeps.renew_sweep(ctx) == "renew=0 recreate=0"
    assert await h.subjects(alice) == ["Board prep"]
    # Resync is refused and direct jobs no-op without calling Graph.
    assert (await alice.post(f"/api/sources/{source.id}/resync")).status_code == 409
    await h.services.queue.enqueue("sync_source", str(source.id), "full")
    assert await h.run_jobs() == [("sync_source", "inactive")]

    # Changes made while paused aren't delivered (the grant is gone)...
    await h.create_source_event("alice@contoso.example", "While paused")
    assert h.queue.jobs == []
    assert h.mock.deliveries[-1].kind == "suspended"
    # ...and a second failure doesn't add another audit/sync error.
    from app.sync.lifecycle import mark_reauth_required

    assert await mark_reauth_required(h.services, source.id, "again") == "reauth-required"
    async with h.services.sessionmaker() as session:
        assert len((await session.scalars(select(SyncError.id))).all()) == 1


async def test_reauth_reactivates_resubscribes_and_resyncs(harness: Harness, alice):
    h = harness
    source = await _paused_by_revocation(h, alice)
    old_token = source.refresh_token
    [old_sub] = await h.subscription_ids()
    await h.create_source_event("alice@contoso.example", "While paused")
    await h.run_jobs()
    assert await h.subjects(alice) == ["Board prep"]

    # Reconnect goes straight to Contoso's own login page with Alice's account pre-filled.
    start = await alice.get(f"/auth/reauth/{source.id}")
    location = start.headers["location"]
    assert location.startswith(f"http://login.test/{tenant_of(h.mock, 'alice@contoso.example')}/oauth2/")
    assert "login_hint=alice%40contoso.example" in location and "Calendars.Read" in location

    result = await h.reauth(alice, source.id, "alice@contoso.example")
    assert result == {"status": "reconnected", "email": "alice@contoso.example"}
    source = await h.source("alice@contoso.example")
    assert source.status == SourceStatus.ACTIVE
    assert source.reauth_required_at is None and source.last_error is None
    assert source.refresh_token and source.refresh_token != old_token

    ran = await h.run_jobs()
    assert ("setup_source", "subscribed") in ran
    assert any(name == "sync_source" and "changed=1" in str(r) for name, r in ran), ran
    [new_sub] = await h.subscription_ids()
    assert new_sub != old_sub and old_sub not in h.mock.subscriptions and new_sub in h.mock.subscriptions
    assert await h.subjects(alice) == ["Board prep", "While paused"]

    # Notifications flow again on the new subscription.
    await h.create_source_event("alice@contoso.example", "After reconnect", hours_from_now=30)
    await h.run_jobs()
    assert "After reconnect" in await h.subjects(alice)
    [out] = (await alice.get("/api/sources")).json()
    assert out["status"] == "active" and out["reauth_url"] is None


async def test_reauth_with_wrong_account_is_rejected(harness: Harness, alice):
    h = harness
    source = await _paused_by_revocation(h, alice)
    result = await h.reauth(alice, source.id, "bob@contoso.example")
    assert result["status"] == "error"
    assert result["message"] == (
        "You signed in as bob@contoso.example. Sign in as alice@contoso.example to reconnect this calendar."
    )
    # An account from another tenant can't even complete sign-in on Contoso's login page.
    result = await h.reauth(alice, source.id, "alice@fabrikam.example")
    assert result["status"] == "error"
    source = await h.source("alice@contoso.example")
    assert source.status == SourceStatus.REAUTH_REQUIRED
    assert h.queue.jobs == []

    # Another platform user can't reconnect (or probe) Alice's source.
    bob = await h.sign_in("bob@contoso.example")
    resp = await bob.get(f"/auth/reauth/{source.id}")
    assert h.result_of(resp) == {"status": "error", "message": "Calendar not found"}
    await bob.aclose()


async def test_linking_again_reconnects_a_paused_source(harness: Harness, alice):
    h = harness
    await _paused_by_revocation(h, alice)
    assert await h.link(alice, "alice@contoso.example") == {
        "status": "reconnected",
        "email": "alice@contoso.example",
    }
    await h.run_jobs()
    assert (await h.source("alice@contoso.example")).status == SourceStatus.ACTIVE
    assert len(h.mock.subscriptions) == 1
    # Re-linking an already active source is just "linked".
    assert (await h.link(alice, "alice@contoso.example"))["status"] == "linked"


async def test_withdrawn_consent_marks_reauth_required(harness: Harness, alice):
    h = harness
    await h.create_source_event("alice@contoso.example", "Keep")
    await link_and_sync(h, alice, "alice@contoso.example")
    owner = h.mock.user_by_upn("alice@contoso.example")
    await h.mock.revoke_user(owner, withdraw_consent=True)
    await h.run_jobs()
    source = await h.source("alice@contoso.example")
    assert source.status == SourceStatus.REAUTH_REQUIRED and "AADSTS65001" in source.last_error
    assert await h.subjects(alice) == ["Keep"]


async def test_graph_403_on_own_mailbox_marks_reauth_required(harness: Harness, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    source = await h.source("alice@contoso.example")
    owner = h.mock.user_by_upn("alice@contoso.example")
    # A token that no longer carries Calendars.Read (e.g. the grant was reduced).
    weak = h.mock.sign(
        {
            "kind": "delegated",
            "tid": owner.tenant_id,
            "oid": owner.oid,
            "appid": h.settings.source_app_client_id,
            "scp": "User.Read",
            "gen": owner.session_generation,
        }
    )
    await h.services.tokens.cache_source_token(source.id, weak, 3600)
    await h.services.queue.enqueue("sync_source", str(source.id), "full")
    assert await h.run_jobs() == [("sync_source", "reauth-required")]
    source = await h.source("alice@contoso.example")
    assert source.status == SourceStatus.REAUTH_REQUIRED and "403" in source.last_error


async def test_paused_source_can_still_be_unlinked(harness: Harness, alice):
    h = harness
    source = await _paused_by_revocation(h, alice)
    assert (await alice.delete(f"/api/sources/{source.id}")).status_code == 202
    await h.run_jobs()
    assert await h.calendar(alice) == []
    assert await h.source_event_count() == 0
    assert (await alice.get("/api/sources")).json() == []


async def test_missed_notifications_are_recovered_by_delta(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    tenant_id = tenant_of(h.mock, "alice@contoso.example")
    drift_before = REGISTRY.get_sample_value("calsync_delta_drift_total", {"tenant": tenant_id}) or 0

    h.mock.tenants[tenant_id].drop_notifications = True
    await h.create_source_event("alice@contoso.example", "Lost in transit")
    await h.run_jobs()
    assert await h.calendar(alice) == []

    await h.mock.send_lifecycle(tenant_id, "missed")
    await h.run_jobs()
    assert await h.subjects(alice) == ["Lost in transit"]
    drift_after = REGISTRY.get_sample_value("calsync_delta_drift_total", {"tenant": tenant_id})
    assert drift_after == drift_before + 1


async def test_subscription_removed_is_recreated(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    tenant_id = tenant_of(h.mock, "alice@contoso.example")
    old_id = next(iter(h.mock.subscriptions))

    await h.mock.send_lifecycle(tenant_id, "subscriptionRemoved")
    await h.run_jobs()
    assert len(h.mock.subscriptions) == 1 and old_id not in h.mock.subscriptions

    await h.create_source_event("alice@contoso.example", "After recreate")
    await h.run_jobs()
    assert await h.subjects(alice) == ["After recreate"]


async def test_reauthorization_required(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    old_token = (await h.source("alice@contoso.example")).refresh_token
    await h.mock.send_lifecycle(tenant_of(h.mock, "alice@contoso.example"), "reauthorizationRequired")
    ran = await h.run_jobs()
    assert ("handle_lifecycle", "reauthorized") in ran
    # Reauthorized with a freshly refreshed delegated token (rotation persisted).
    source = await h.source("alice@contoso.example")
    assert source.status == SourceStatus.ACTIVE and source.refresh_token != old_token


async def test_renewal_extends_and_recreates(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    ctx = {"services": h.services}

    # Nothing due right after creation.
    assert (await sweeps.renew_sweep(ctx)).startswith("renew=0")

    async with h.services.sessionmaker() as session:
        await session.execute(update(Subscription).values(renew_due_at=utcnow() - timedelta(minutes=1)))
        await session.commit()
    assert (await sweeps.renew_sweep(ctx)).startswith("renew=1")
    ran = await h.run_jobs()
    assert ("renew_subscription", "renewed") in ran
    async with h.services.sessionmaker() as session:
        sub = await session.scalar(select(Subscription))
        assert sub.renew_due_at > utcnow() + timedelta(hours=23)
        assert sub.expires_at > utcnow() + timedelta(hours=69)

    # Subscription silently gone at Graph: renewal recreates it and resyncs.
    h.mock.subscriptions.clear()
    async with h.services.sessionmaker() as session:
        await session.execute(update(Subscription).values(renew_due_at=utcnow() - timedelta(minutes=1)))
        await session.commit()
    await sweeps.renew_sweep(ctx)
    ran = await h.run_jobs()
    assert ("renew_subscription", "recreated") in ran
    assert len(h.mock.subscriptions) == 1


async def test_sources_without_subscription_get_one(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    async with h.services.sessionmaker() as session:
        await session.execute(Subscription.__table__.delete())
        await session.execute(update(LinkedSource).values(created_at=utcnow() - timedelta(hours=1)))
        await session.commit()
    assert (await sweeps.renew_sweep({"services": h.services})).endswith("recreate=1")
    await h.run_jobs()
    async with h.services.sessionmaker() as session:
        assert await session.scalar(select(Subscription)) is not None


async def test_scheduled_delta_catches_silent_changes(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    tenant_id = tenant_of(h.mock, "alice@contoso.example")
    h.mock.tenants[tenant_id].drop_notifications = True
    event = await h.create_source_event("alice@contoso.example", "Silent")
    await h.run_jobs()

    async with h.services.sessionmaker() as session:
        await session.execute(update(LinkedSource).values(last_synced_at=utcnow() - timedelta(hours=7)))
        await session.commit()
    assert await sweeps.delta_sweep({"services": h.services}) == "queued=1"
    await h.run_jobs()
    assert await h.subjects(alice) == ["Silent"]

    # A deletion that was never notified disappears on the next delta too.
    await h.mock.delete_event(h.mock.user_by_upn("alice@contoso.example"), event["id"])
    async with h.services.sessionmaker() as session:
        await session.execute(update(LinkedSource).values(last_synced_at=utcnow() - timedelta(hours=7)))
        await session.commit()
    await sweeps.delta_sweep({"services": h.services})
    await h.run_jobs()
    assert await h.calendar(alice) == []


async def test_rebaseline_full_delta_and_pagination(harness: Harness, operator, alice):
    h = harness
    h.mock.max_page_size = 2
    for i in range(5):
        await h.create_source_event("alice@contoso.example", f"Event {i}", hours_from_now=24 + i)
    await link_and_sync(h, alice, "alice@contoso.example")
    assert len(await h.calendar(alice)) == 5
    sync_results = [r for name, r in h.results if name == "sync_source"]
    assert any("pages=3" in str(r) for r in sync_results)

    [source] = (await alice.get("/api/sources")).json()
    assert (await alice.post(f"/api/sources/{source['id']}/resync")).status_code == 202
    ran = await h.run_jobs()
    assert ("sync_source", "pages=3 changed=0 full=True") in ran
    assert len(await h.calendar(alice)) == 5


async def test_expired_delta_token_rebaselines(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    # A delta token Graph no longer recognises (410 Gone / resync required).
    oid = h.mock.user_by_upn("alice@contoso.example").oid
    stale = b64({"cal": "expired", "seq": 0, "start": "2026-01-01T00:00:00Z", "end": "2027-01-01T00:00:00Z"})
    async with h.services.sessionmaker() as session:
        await session.execute(
            update(LinkedSource).values(
                delta_link=f"http://graph.test/v1.0/users/{oid}/calendarView/delta?$deltatoken={stale}"
            )
        )
        await session.commit()
    await h.create_source_event("alice@contoso.example", "After expiry")
    h.queue.jobs.clear()
    [source] = (await alice.get("/api/sources")).json()
    await h.services.queue.enqueue("sync_source", source["id"], "recovery")
    ran = await h.run_jobs()
    assert any(name == "sync_source" and "full=True" in str(r) for name, r in ran)
    assert await h.subjects(alice) == ["After expiry"]


async def test_throttling_defers_and_recovers(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    tenant_id = tenant_of(h.mock, "alice@contoso.example")

    h.mock.tenants[tenant_id].throttle_remaining = 1
    await h.create_source_event("alice@contoso.example", "Busy tenant")
    ran = await h.run_jobs()
    assert ("process_notification", "deferred") in ran
    assert await h.services.redis.exists(f"throttle:tenant:{tenant_id}")
    assert await h.calendar(alice) == []

    await h.release_deferred()
    await h.run_jobs()
    assert await h.subjects(alice) == ["Busy tenant"]


async def test_time_zone_converted_for_the_calendar(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    day = (utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
    await h.mock.create_event(
        h.mock.user_by_upn("alice@contoso.example"),
        {
            "subject": "West coast call",
            "start": {"dateTime": f"{day}T09:00:00", "timeZone": "Pacific Standard Time"},
            "end": {"dateTime": f"{day}T10:00:00", "timeZone": "Pacific Standard Time"},
        },
    )
    await h.run_jobs()
    [event] = await h.calendar(alice)
    expected = datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    assert at(event["start"]) == expected.astimezone(UTC)
    assert at(event["end"]) == expected.astimezone(UTC) + timedelta(hours=1)
    async with h.services.sessionmaker() as session:
        stored = await session.scalar(select(SourceEvent))
        assert stored.start_tz == "Pacific Standard Time"


async def test_all_day_event_keeps_its_date(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    day = (utcnow() + timedelta(days=4)).date()
    await h.mock.create_event(
        h.mock.user_by_upn("alice@contoso.example"),
        {
            "subject": "Offsite",
            "isAllDay": True,
            "start": {"dateTime": f"{day}T00:00:00", "timeZone": "Pacific Standard Time"},
            "end": {"dateTime": f"{day + timedelta(days=1)}T00:00:00", "timeZone": "Pacific Standard Time"},
        },
    )
    await h.run_jobs()
    [event] = await h.calendar(alice)
    assert event["is_all_day"] and at(event["start"]).date() == day


async def test_privacy_mode_shows_busy_only(harness: Harness, operator, alice):
    h = harness
    secret = await h.create_source_event(
        "alice@contoso.example", "Secret merger", location={"displayName": "Vault"}
    )
    await link_and_sync(h, alice, "alice@contoso.example")
    assert await h.subjects(alice) == ["Secret merger"]

    # "Hide details" applies as soon as it's set: the calendar reads it every time.
    [tenant] = (await operator.get("/api/admin/tenants")).json()
    resp = await operator.patch(f"/api/admin/tenants/{tenant['id']}", json={"copy_details": False})
    assert resp.status_code == 200 and resp.json()["copy_details"] is False
    assert h.queue.jobs == []
    [event] = await h.calendar(alice)
    assert event["subject"] == "Busy" and event["location"] == "" and event["labels"] == ["Contoso"]

    # The same meeting from a tenant that allows details still shows "Busy".
    await link_and_sync(h, alice, "alice@fabrikam.example")
    await h.create_source_event(
        "alice@fabrikam.example",
        "Secret merger",
        location={"displayName": "Vault"},
        ical_uid=secret["iCalUId"],
    )
    await h.run_jobs()
    [event] = await h.calendar(alice)
    assert event["subject"] == "Busy" and event["labels"] == ["Contoso", "Fabrikam"]

    await operator.patch(f"/api/admin/tenants/{tenant['id']}", json={"copy_details": True})
    [event] = await h.calendar(alice)
    assert event["subject"] == "Secret merger" and event["location"] == "Vault"


async def test_label_rename_shows_in_calendar(harness: Harness, operator, alice):
    h = harness
    await h.create_source_event("alice@contoso.example", "Sync")
    await link_and_sync(h, alice, "alice@contoso.example")
    [source] = (await alice.get("/api/sources")).json()
    resp = await alice.patch(f"/api/sources/{source['id']}", json={"label": "Client A"})
    assert resp.status_code == 200
    assert resp.json()["label"] == "Client A" and resp.json()["event_count"] == 1
    assert h.queue.jobs == []  # nothing to re-render
    [event] = await h.calendar(alice)
    assert event["labels"] == ["Client A"] and event["subject"] == "Sync"


async def test_users_are_isolated(harness: Harness, operator, alice):
    h = harness
    await h.create_source_event("alice@contoso.example", "Alice only")
    await link_and_sync(h, alice, "alice@contoso.example")

    bob = await h.sign_in("bob@contoso.example")
    [alice_source] = (await alice.get("/api/sources")).json()
    assert (await bob.get("/api/sources")).json() == []
    assert (await bob.delete(f"/api/sources/{alice_source['id']}")).status_code == 404
    assert (await bob.patch(f"/api/sources/{alice_source['id']}", json={"label": "x"})).status_code == 404
    assert await h.calendar(bob) == []

    # Bob can't link Alice's mailbox: linking requires signing in *as* that mailbox, and
    # linking bob's own mailbox gives bob his own, separate calendar.
    await h.create_source_event("bob@contoso.example", "Bob only")
    await link_and_sync(h, bob, "bob@contoso.example")
    assert await h.subjects(alice) == ["Alice only"]
    assert await h.subjects(bob) == ["Bob only"]
    await bob.aclose()


async def test_calendar_range_is_limited(harness: Harness, alice):
    now = utcnow()
    for start, end in ((now, now - timedelta(hours=1)), (now, now + timedelta(days=63))):
        resp = await alice.get("/api/calendar", params={"start": start.isoformat(), "end": end.isoformat()})
        assert resp.status_code == 400
    resp = await alice.get(
        "/api/calendar", params={"start": now.isoformat(), "end": (now + timedelta(days=62)).isoformat()}
    )
    assert resp.status_code == 200 and resp.json() == []


async def test_source_calendars_are_never_written(harness: Harness, operator, alice):
    h = harness
    await h.create_source_event("alice@contoso.example", "Read me")
    await link_and_sync(h, alice, "alice@contoso.example")
    before = {
        eid: dict(ev)
        for cal in h.mock.calendars.values()
        if h.mock.users[cal.owner_oid].upn == "alice@contoso.example"
        for eid, ev in cal.events.items()
    }
    await h.create_source_event("alice@contoso.example", "Another")
    await h.run_jobs()
    [source] = (await alice.get("/api/sources")).json()
    await alice.delete(f"/api/sources/{source['id']}")
    await h.run_jobs()
    after = {
        eid: ev
        for cal in h.mock.calendars.values()
        if h.mock.users[cal.owner_oid].upn == "alice@contoso.example"
        for eid, ev in cal.events.items()
    }
    for eid, ev in before.items():
        assert after[eid] == ev
    assert len(after) == len(before) + 1


async def test_blocked_user_consent_maps_to_admin_approval_required(harness: Harness, alice):
    h = harness
    h.mock.tenants[tenant_of(h.mock, "alice@contoso.example")].user_consent_blocked = True
    result = await h.link(alice, "alice@contoso.example")
    assert result["status"] == "admin_approval_required"
    assert "AADSTS90094" in result["message"]
    assert (await alice.get("/api/sources")).json() == []
    assert h.queue.jobs == []


async def test_declined_consent_is_a_plain_error(harness: Harness, alice):
    async with harness.client() as client:
        resp = await client.get(
            "/auth/callback",
            params={"error": "access_denied", "error_description": "AADSTS65004: User declined to consent."},
        )
    assert harness.result_of(resp) == {"status": "error", "message": "AADSTS65004: User declined to consent."}


async def test_account_deletion_removes_everything(harness: Harness, operator, alice):
    h = harness
    await h.create_source_event("alice@contoso.example", "Bye")
    await link_and_sync(h, alice, "alice@contoso.example")
    await link_and_sync(h, alice, "alice@fabrikam.example")
    sources = [s["id"] for s in (await alice.get("/api/sources")).json()]

    assert (await alice.delete("/api/me")).status_code == 202
    assert await h.run_jobs() == [("delete_user", "deleted")]
    assert h.mock.subscriptions == {}
    assert await h.source_event_count() == 0
    async with h.services.sessionmaker() as session:
        assert (await session.scalars(select(LinkedSource.id))).all() == []
        assert (await session.scalars(select(User.email))).all() == ["ops@calsync.example"]
    for source_id in sources:
        assert not await h.services.redis.exists(f"token:source:{source_id}")
    assert (await alice.get("/api/me")).status_code == 401


async def test_admin_health_views(harness: Harness, operator, alice):
    h = harness
    await link_and_sync(h, alice, "alice@contoso.example")
    await h.create_source_event("alice@contoso.example", "One")
    await h.create_source_event("alice@contoso.example", "Two", hours_from_now=30)
    await h.run_jobs()
    overview = (await operator.get("/api/admin/overview")).json()
    assert overview["users"] == 2
    assert overview["tenants"] == 1
    assert overview["sources"] == {SourceStatus.ACTIVE: 1}
    assert overview["subscriptions"] == 1
    assert overview["events"] == 2 and "mirrors" not in overview
    [tenant] = (await operator.get("/api/admin/tenants")).json()
    assert tenant["active_source_count"] == 1 and tenant["next_subscription_expiry"]
    assert tenant["source_count"] == 1 and tenant["reauth_required_count"] == 0
    assert tenant["tenant_id"] == tenant_of(h.mock, "alice@contoso.example")
    assert not {"consent_status", "connected_at", "revoked_at", "consent_url"} & set(tenant)
    subs = (await operator.get("/api/admin/subscriptions")).json()
    assert subs[0]["user_email"] == "alice@contoso.example"
    audit = (await operator.get("/api/admin/audit")).json()
    assert {"tenant.discovered", "source.linked"} <= {a["action"] for a in audit}
    assert (await alice.get("/api/admin/tenants")).status_code == 403

    # The admin-consent era endpoints are gone.
    assert (await operator.post("/api/admin/tenants", json={"display_name": "X"})).status_code == 405
    assert (await operator.get(f"/api/admin/tenants/{tenant['id']}/consent-link")).status_code == 404
    assert (await operator.post(f"/api/admin/tenants/{tenant['id']}/revoke")).status_code == 404
    assert (await operator.get("/auth/adminconsent/callback")).status_code == 404

    # Paused sources are counted per tenant and in the overview.
    await h.mock.revoke_user(h.mock.user_by_upn("alice@contoso.example"))
    await h.run_jobs()
    [tenant] = (await operator.get("/api/admin/tenants")).json()
    assert tenant["reauth_required_count"] == 1 and tenant["active_source_count"] == 0
    overview = (await operator.get("/api/admin/overview")).json()
    assert overview["sources"] == {SourceStatus.REAUTH_REQUIRED: 1}


async def test_admin_users(harness: Harness, operator, alice):
    h = harness
    await h.create_source_event("alice@contoso.example", "Contoso event")
    await h.create_source_event("alice@fabrikam.example", "Fabrikam event")
    await link_and_sync(h, alice, "alice@contoso.example")
    await link_and_sync(h, alice, "alice@fabrikam.example")

    users = {u["email"]: u for u in (await operator.get("/api/admin/users")).json()}
    assert set(users["alice@contoso.example"]) == {
        "id",
        "email",
        "display_name",
        "is_operator",
        "source_count",
        "event_count",
        "created_at",
    }
    assert users["alice@contoso.example"]["source_count"] == 2
    assert users["alice@contoso.example"]["event_count"] == 2
    assert users["ops@calsync.example"]["event_count"] == 0 and users["ops@calsync.example"]["is_operator"]

    alice_id = users["alice@contoso.example"]["id"]
    resp = await operator.patch(f"/api/admin/users/{alice_id}", json={"is_operator": True})
    assert resp.status_code == 202 and resp.json() == {"status": "updated"}
    assert (await alice.get("/api/me")).json()["is_operator"] is True
    ops_id = users["ops@calsync.example"]["id"]
    assert (
        await operator.patch(f"/api/admin/users/{ops_id}", json={"is_operator": False})
    ).status_code == 400
    assert (await alice.get("/api/admin/users")).status_code == 200

    # The primary-calendar endpoints are gone.
    assert (await operator.post(f"/api/admin/users/{alice_id}/provision")).status_code == 404
    assert (await operator.post(f"/api/admin/users/{alice_id}/rebuild")).status_code == 404
