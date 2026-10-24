"""Source and account lifecycle: setup, reauth_required, unlink, account deletion."""

import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.audit import audit
from app.models import LinkedSource, SourceEvent, SourceStatus, SyncError, User
from app.queue import sync_key
from app.services import Services
from app.sync.source_sync import SyncMode
from app.sync.subscriptions import create_subscription, delete_remote_subscription
from app.timeutil import utcnow

log = logging.getLogger(__name__)


# --- sources -------------------------------------------------------------------------


async def setup_source(services: Services, source_id: uuid.UUID, mode: SyncMode = SyncMode.INITIAL) -> str:
    """Start syncing a newly linked or reconnected source: subscribe, then sync.

    INITIAL backfills a new link; RECOVERY (after a reconnect) continues from the stored
    delta link, which picks up everything that changed while the source was paused.
    """
    async with services.sessionmaker() as session:
        source = await session.get(LinkedSource, source_id)
        if source is None:
            return "gone"
        if source.status != SourceStatus.ACTIVE:
            return f"skipped-{source.status}"

    # Subscribe first, then sync: a change made in between is caught by the delta.
    # create_subscription replaces a subscription left over from a previous grant.
    await create_subscription(services, source_id)
    await services.queue.enqueue_coalesced(
        sync_key(source_id, mode), "sync_source", str(source_id), mode.value
    )
    return "subscribed"


async def mark_reauth_required(services: Services, source_id: uuid.UUID, reason: str) -> str:
    """The source's delegated grant stopped working (refresh token expired or revoked, MFA
    or consent now required, password reset...). Pause syncing until the owner signs in
    again. Stored events (still shown in the unified calendar) and the subscription row
    are kept on purpose."""
    async with services.sessionmaker() as session:
        source = await session.get(LinkedSource, source_id)
        if source is None:
            return "gone"
        if source.status == SourceStatus.REMOVING:
            return "removing"
        now = utcnow()
        first = source.status != SourceStatus.REAUTH_REQUIRED
        source.status = SourceStatus.REAUTH_REQUIRED
        source.last_error = reason[:2000]
        source.last_error_at = now
        if first:
            source.reauth_required_at = now
            audit(
                session,
                "source.reauth_required",
                "linked_source",
                source.id,
                email=source.source_email,
                reason=reason[:500],
            )
            session.add(
                SyncError(
                    user_id=source.user_id,
                    tenant_pk=source.tenant_pk,
                    source_id=source.id,
                    kind="source.reauth_required",
                    message=reason[:4000],
                )
            )
        await session.commit()
    await services.tokens.invalidate_source(source_id)
    if first:
        log.warning("source %s needs the user to reconnect: %s", source_id, reason)
    return "reauth-required"


async def purge_source(services: Services, source_id: uuid.UUID, *, reason: str) -> str:
    """Unlink: stop syncing a source, delete its stored events, which removes them from
    the unified calendar (FR-12), and delete the link, including its refresh token."""
    async with services.sessionmaker() as session:
        source = await session.get(
            LinkedSource,
            source_id,
            options=[selectinload(LinkedSource.tenant), selectinload(LinkedSource.subscription)],
        )
        if source is None:
            return "gone"
        tenant_id = source.tenant.tenant_id
        # Hide it from the unified calendar immediately.
        source.status = SourceStatus.REMOVING
        await session.commit()

        if source.subscription is not None:
            if tenant_id:
                # Best effort, with the source's own token (fails harmlessly if revoked).
                await delete_remote_subscription(
                    services, source.id, tenant_id, source.subscription.graph_subscription_id
                )
            await session.delete(source.subscription)

        events = len(
            (
                await session.scalars(
                    delete(SourceEvent).where(SourceEvent.source_id == source_id).returning(SourceEvent.id)
                )
            ).all()
        )
        source.refresh_token = None
        await session.delete(source)
        audit(session, "source.purged", "linked_source", source_id, reason=reason, events=events)
        await session.commit()

    await services.tokens.invalidate_source(source_id)
    return f"purged events={events}"


# --- accounts ------------------------------------------------------------------------


async def delete_user(services: Services, user_id: uuid.UUID) -> str:
    """Account deletion: stop every subscription and delete all stored data for the user."""
    async with services.sessionmaker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return "gone"
        sources = (
            await session.scalars(
                select(LinkedSource)
                .where(LinkedSource.user_id == user_id)
                .options(selectinload(LinkedSource.tenant), selectinload(LinkedSource.subscription))
            )
        ).all()
        for source in sources:
            source.status = SourceStatus.REMOVING
            if source.subscription is not None and source.tenant.tenant_id:
                await delete_remote_subscription(
                    services, source.id, source.tenant.tenant_id, source.subscription.graph_subscription_id
                )
        await session.commit()
        for source in sources:
            await services.tokens.invalidate_source(source.id)

        audit(session, "user.deleted", "user", user_id, email=user.email)
        # Linked sources, events and subscriptions cascade in the database.
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
    return "deleted"
