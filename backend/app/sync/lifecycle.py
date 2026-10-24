"""Source lifecycle (setup, reauth_required, unlink) and primary calendar provisioning."""

import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.audit import audit
from app.graph.client import GraphError, GraphNotFound
from app.models import (
    LinkedSource,
    MirrorMapping,
    PrimaryStatus,
    SourceEvent,
    SourceStatus,
    SyncError,
    User,
)
from app.queue import sync_key
from app.services import Services
from app.sync.mirror import all_dedup_keys
from app.sync.source_sync import SyncMode, enqueue_reconciles
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
    again. Mirrors, stored events and the subscription row are kept on purpose."""
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
    """Unlink: stop syncing a source, remove its mirrors (FR-12) and delete the link,
    including its stored refresh token."""
    async with services.sessionmaker() as session:
        source = await session.get(
            LinkedSource,
            source_id,
            options=[selectinload(LinkedSource.tenant), selectinload(LinkedSource.subscription)],
        )
        if source is None:
            return "gone"
        tenant_id = source.tenant.tenant_id
        user_id = source.user_id
        # Exclude it from every mirror group immediately.
        source.status = SourceStatus.REMOVING
        await session.commit()

        if source.subscription is not None:
            if tenant_id:
                # Best effort, with the source's own token (fails harmlessly if revoked).
                await delete_remote_subscription(
                    services, source.id, tenant_id, source.subscription.graph_subscription_id
                )
            await session.delete(source.subscription)

        keys = set(
            (
                await session.scalars(
                    delete(SourceEvent)
                    .where(SourceEvent.source_id == source_id)
                    .returning(SourceEvent.dedup_key)
                )
            ).all()
        )
        source.refresh_token = None
        await session.delete(source)
        audit(session, "source.purged", "linked_source", source_id, reason=reason, mirrors=len(keys))
        await session.commit()

    await services.tokens.invalidate_source(source_id)
    await enqueue_reconciles(services, user_id, keys)
    return f"purged mirrors={len(keys)}"


# --- primary calendars ---------------------------------------------------------------


async def provision_primary(services: Services, user_id: uuid.UUID) -> str:
    """FR-2: give the user one primary calendar in our tenant."""
    settings = services.settings
    primary = services.graph.primary()
    async with services.sessionmaker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return "gone"
        try:
            if settings.primary_calendar_mode == "shared_mailbox":
                mailbox = settings.primary_shared_mailbox
                if not mailbox:
                    raise RuntimeError("PRIMARY_SHARED_MAILBOX is not configured")
                calendar_id = user.primary_calendar_id if user.primary_mailbox == mailbox else None
                if calendar_id:
                    try:
                        await primary.get_calendar(mailbox, calendar_id)
                    except GraphNotFound:
                        calendar_id = None
                if not calendar_id:
                    created = await primary.create_calendar(
                        mailbox, f"{user.display_name or user.email} ({user.email})"
                    )
                    calendar_id = created["id"]
            else:
                mailbox = user.primary_mailbox
                if not mailbox:
                    user.primary_status = PrimaryStatus.UNASSIGNED
                    await session.commit()
                    return "unassigned"
                calendar_id = (await primary.get_default_calendar(mailbox))["id"]
        except (GraphError, RuntimeError) as exc:
            if isinstance(exc, GraphError) and exc.transient:
                raise
            user.primary_status = PrimaryStatus.ERROR
            user.primary_error = str(exc)
            await session.commit()
            raise

        changed = (user.primary_mailbox, user.primary_calendar_id) != (mailbox, calendar_id)
        user.primary_mailbox, user.primary_calendar_id = mailbox, calendar_id
        user.primary_status = PrimaryStatus.READY
        user.primary_error = None
        audit(session, "primary.provisioned", "user", user.id, mailbox=mailbox, changed=changed)
        await session.commit()
        keys = await all_dedup_keys(session, user_id)

    # Mirror everything already synced (no-ops for groups that are already current).
    await enqueue_reconciles(services, user_id, keys)
    return f"ready mirrors={len(keys)}"


async def delete_user(services: Services, user_id: uuid.UUID) -> str:
    """Account deletion: remove every mirror and all stored data for the user."""
    settings = services.settings
    primary = services.graph.primary()
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

        # Deleted progressively so a throttled retry resumes where it left off.
        mappings = (
            await session.scalars(select(MirrorMapping).where(MirrorMapping.user_id == user_id))
        ).all()
        for mapping in mappings:
            if mapping.graph_event_id and mapping.primary_mailbox:
                try:
                    await primary.delete_event(mapping.primary_mailbox, mapping.graph_event_id)
                except GraphNotFound:
                    pass
            await session.delete(mapping)
            await session.commit()

        if (
            settings.primary_calendar_mode == "shared_mailbox"
            and user.primary_mailbox
            and user.primary_calendar_id
        ):
            try:
                await primary.delete_calendar(user.primary_mailbox, user.primary_calendar_id)
            except GraphNotFound:
                pass

        audit(session, "user.deleted", "user", user_id, email=user.email)
        # Linked sources, events, subscriptions and mappings cascade in the database.
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
    return "deleted"
