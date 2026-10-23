"""Graph change-notification subscriptions: create, renew, reauthorize, recreate."""

import logging
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.graph.client import GraphError, GraphNotFound
from app.graph.tokens import TokenError
from app.metrics import RENEWAL_FAILURES, SUBSCRIPTIONS_RECREATED
from app.models import LinkedSource, SourceStatus, Subscription, SubscriptionStatus
from app.queue import sync_key
from app.services import Services
from app.sync.events import parse_iso
from app.sync.source_sync import SyncMode
from app.timeutil import isoformat_z, utcnow

log = logging.getLogger(__name__)

RENEW_RETRY = timedelta(minutes=15)


async def _load_source(session: AsyncSession, source_id: uuid.UUID) -> LinkedSource | None:
    return await session.get(
        LinkedSource,
        source_id,
        options=[selectinload(LinkedSource.tenant), selectinload(LinkedSource.subscription)],
    )


async def delete_remote_subscription(services: Services, tenant_id: str, graph_subscription_id: str) -> None:
    """Best effort: the subscription may already be gone, or the tenant may have revoked us."""
    try:
        await services.graph.source(tenant_id).delete_subscription(graph_subscription_id)
    except (GraphError, TokenError) as exc:
        log.info("could not delete subscription %s: %s", graph_subscription_id, exc)


async def create_subscription(services: Services, source_id: uuid.UUID) -> Subscription | None:
    """Create (or replace) the subscription for a source calendar."""
    settings = services.settings
    async with services.sessionmaker() as session:
        source = await _load_source(session, source_id)
        if source is None or source.status != SourceStatus.ACTIVE or not source.tenant.tenant_id:
            return None
        tenant_id = source.tenant.tenant_id
        graph = services.graph.source(tenant_id)

        if source.subscription is not None:
            old = source.subscription
            await delete_remote_subscription(services, tenant_id, old.graph_subscription_id)
            await session.delete(old)
            await session.flush()

        now = utcnow()
        client_state = secrets.token_urlsafe(32)
        resource = graph.subscription_resource(source.source_user_id, source.calendar_id)
        api = settings.public_api_url.rstrip("/")
        created = await graph.create_subscription(
            {
                "changeType": "created,updated,deleted",
                "notificationUrl": f"{api}/webhooks/graph",
                "lifecycleNotificationUrl": f"{api}/webhooks/lifecycle",
                "resource": resource,
                "expirationDateTime": isoformat_z(
                    now + timedelta(minutes=settings.subscription_lifetime_minutes)
                ),
                "clientState": client_state,
            }
        )
        sub = Subscription(
            source_id=source.id,
            graph_subscription_id=created["id"],
            client_state=client_state,
            resource=resource,
            expires_at=parse_iso(created.get("expirationDateTime"))
            or now + timedelta(minutes=settings.subscription_lifetime_minutes),
            renew_due_at=now + timedelta(hours=settings.subscription_renew_interval_hours),
            last_renewed_at=now,
        )
        session.add(sub)
        await session.commit()
        log.info("created subscription %s for source %s", sub.graph_subscription_id, source.id)
        return sub


async def recreate_and_resync(services: Services, source_id: uuid.UUID, tenant_id: str) -> None:
    SUBSCRIPTIONS_RECREATED.labels(tenant_id).inc()
    await create_subscription(services, source_id)
    await services.queue.enqueue_coalesced(
        sync_key(source_id, SyncMode.RECOVERY), "sync_source", str(source_id), SyncMode.RECOVERY.value
    )


async def renew_subscription(services: Services, subscription_pk: uuid.UUID) -> str:
    settings = services.settings
    async with services.sessionmaker() as session:
        sub = await session.get(
            Subscription,
            subscription_pk,
            options=[selectinload(Subscription.source).selectinload(LinkedSource.tenant)],
        )
        if sub is None:
            return "gone"
        source = sub.source
        tenant_id = source.tenant.tenant_id
        assert tenant_id
        if source.status != SourceStatus.ACTIVE:
            await delete_remote_subscription(services, tenant_id, sub.graph_subscription_id)
            await session.delete(sub)
            await session.commit()
            return "source-inactive"

        now = utcnow()
        try:
            renewed = await services.graph.source(tenant_id).renew_subscription(
                sub.graph_subscription_id, now + timedelta(minutes=settings.subscription_lifetime_minutes)
            )
        except GraphNotFound:
            RENEWAL_FAILURES.labels(tenant_id).inc()
            log.warning("subscription %s vanished; recreating", sub.graph_subscription_id)
            source_id = source.id
            await session.commit()
            await recreate_and_resync(services, source_id, tenant_id)
            return "recreated"
        except GraphError as exc:
            if exc.transient:
                raise
            RENEWAL_FAILURES.labels(tenant_id).inc()
            sub.status = SubscriptionStatus.RENEW_FAILED
            sub.last_error = str(exc)
            sub.renew_due_at = now + RENEW_RETRY
            await session.commit()
            raise

        expires = parse_iso((renewed or {}).get("expirationDateTime"))
        sub.expires_at = expires or now + timedelta(minutes=settings.subscription_lifetime_minutes)
        sub.renew_due_at = now + timedelta(hours=settings.subscription_renew_interval_hours)
        sub.last_renewed_at = now
        sub.status = SubscriptionStatus.ACTIVE
        sub.last_error = None
        await session.commit()
        return "renewed"


async def handle_lifecycle(services: Services, graph_subscription_id: str, event: str) -> str:
    async with services.sessionmaker() as session:
        sub = await session.scalar(
            select(Subscription)
            .where(Subscription.graph_subscription_id == graph_subscription_id)
            .options(selectinload(Subscription.source).selectinload(LinkedSource.tenant))
        )
        if sub is None:
            return "unknown-subscription"
        source_id, sub_pk = sub.source_id, sub.id
        tenant_id = sub.source.tenant.tenant_id
        assert tenant_id

    if event == "missed":
        await services.queue.enqueue_coalesced(
            sync_key(source_id, SyncMode.RECOVERY), "sync_source", str(source_id), SyncMode.RECOVERY.value
        )
        return "resync"
    if event == "subscriptionRemoved":
        await recreate_and_resync(services, source_id, tenant_id)
        return "recreated"
    if event == "reauthorizationRequired":
        try:
            await services.graph.source(tenant_id).reauthorize_subscription(graph_subscription_id)
            return "reauthorized"
        except GraphNotFound:
            await recreate_and_resync(services, source_id, tenant_id)
            return "recreated"
        except GraphError as exc:
            if exc.transient:
                raise
            log.info("reauthorize failed for %s (%s); renewing instead", graph_subscription_id, exc)
            return await renew_subscription(services, sub_pk)
    log.warning("ignoring unknown lifecycle event %s", event)
    return "ignored"
