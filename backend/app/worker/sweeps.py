"""Periodic sweeps run by the scheduler. Each one only *enqueues* work, so they're cheap
and safe to run on several scheduler replicas (arq cron jobs are unique per run)."""

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select

from app.models import LinkedSource, SourceStatus, Subscription
from app.queue import renew_key, sync_key
from app.services import Services
from app.sync.source_sync import SyncMode
from app.timeutil import utcnow

log = logging.getLogger(__name__)

RENEW_EXPIRY_MARGIN = timedelta(hours=12)
SETUP_GRACE = timedelta(minutes=10)


async def renew_sweep(ctx: dict[str, Any]) -> str:
    """FR-6: renew every subscription before it expires; recreate missing ones. Only for
    active sources: a source waiting for its owner to reconnect is left alone."""
    services: Services = ctx["services"]
    now = utcnow()
    async with services.sessionmaker() as session:
        due = (
            await session.scalars(
                select(Subscription.id)
                .join(LinkedSource, LinkedSource.id == Subscription.source_id)
                .where(
                    LinkedSource.status == SourceStatus.ACTIVE,
                    or_(
                        Subscription.renew_due_at <= now, Subscription.expires_at <= now + RENEW_EXPIRY_MARGIN
                    ),
                )
            )
        ).all()
        missing = (
            await session.scalars(
                select(LinkedSource.id)
                .outerjoin(Subscription, Subscription.source_id == LinkedSource.id)
                .where(
                    LinkedSource.status == SourceStatus.ACTIVE,
                    Subscription.id.is_(None),
                    LinkedSource.created_at < now - SETUP_GRACE,
                )
            )
        ).all()
    for pk in due:
        await services.queue.enqueue_coalesced(renew_key(pk), "renew_subscription", str(pk))
    for source_id in missing:
        await services.queue.enqueue_coalesced(f"ensure:{source_id}", "ensure_subscription", str(source_id))
    return f"renew={len(due)} recreate={len(missing)}"


async def delta_sweep(ctx: dict[str, Any]) -> str:
    """FR-7: periodic safety-net delta for every active source."""
    services: Services = ctx["services"]
    cutoff = utcnow() - timedelta(hours=services.settings.delta_resync_interval_hours)
    async with services.sessionmaker() as session:
        due = (
            await session.scalars(
                select(LinkedSource.id).where(
                    LinkedSource.status == SourceStatus.ACTIVE,
                    or_(LinkedSource.last_synced_at.is_(None), LinkedSource.last_synced_at < cutoff),
                )
            )
        ).all()
    for source_id in due:
        await services.queue.enqueue_coalesced(
            sync_key(source_id, SyncMode.SCHEDULED), "sync_source", str(source_id), SyncMode.SCHEDULED.value
        )
    return f"queued={len(due)}"
