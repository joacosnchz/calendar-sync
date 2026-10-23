"""Linking, unlinking and revocation of sources and tenants; primary calendar provisioning."""

import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.audit import audit
from app.graph.client import GraphError, GraphNotFound
from app.models import (
    ConsentStatus,
    LinkedSource,
    MirrorMapping,
    PrimaryStatus,
    SourceEvent,
    SourceStatus,
    Tenant,
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


async def setup_source(services: Services, source_id: uuid.UUID) -> str:
    """Start syncing a newly linked (or re-activated) source: subscribe, then backfill."""
    async with services.sessionmaker() as session:
        source = await session.get(LinkedSource, source_id, options=[selectinload(LinkedSource.tenant)])
        if source is None:
            return "gone"
        if source.tenant.consent_status != ConsentStatus.CONNECTED:
            source.status = SourceStatus.PENDING_CONSENT
            await session.commit()
            return "pending-consent"
        if source.status != SourceStatus.ACTIVE:
            return f"skipped-{source.status}"

    # Subscribe first, then backfill: a change made in between is caught by the delta.
    await create_subscription(services, source_id)
    await services.queue.enqueue_coalesced(
        sync_key(source_id, SyncMode.INITIAL), "sync_source", str(source_id), SyncMode.INITIAL.value
    )
    return "subscribed"


async def purge_source(
    services: Services, source_id: uuid.UUID, *, final_status: str | None, reason: str
) -> str:
    """Stop syncing a source and remove its mirrors (FR-12).

    final_status=None deletes the link (user unlinked); otherwise the link is kept with
    that status so the user can see why it stopped (e.g. tenant revoked).
    """
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
        source.status = final_status or SourceStatus.REMOVING
        await session.commit()

        if source.subscription is not None:
            if tenant_id:
                await delete_remote_subscription(
                    services, tenant_id, source.subscription.graph_subscription_id
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
        source.delta_link = None
        source.delta_window_start = source.delta_window_end = None
        if final_status is None:
            await session.delete(source)
        audit(session, "source.purged", "linked_source", source_id, reason=reason, mirrors=len(keys))
        await session.commit()

    await enqueue_reconciles(services, user_id, keys)
    return f"purged mirrors={len(keys)}"


# --- tenants -------------------------------------------------------------------------


async def revoke_tenant(services: Services, tenant_pk: uuid.UUID, reason: str) -> str:
    async with services.sessionmaker() as session:
        tenant = await session.get(Tenant, tenant_pk)
        if tenant is None:
            return "gone"
        if tenant.consent_status != ConsentStatus.REVOKED:
            tenant.consent_status = ConsentStatus.REVOKED
            tenant.revoked_at = utcnow()
        tenant.last_error = reason
        audit(session, "tenant.revoked", "tenant", tenant.id, reason=reason)
        source_ids = list(
            (
                await session.scalars(
                    select(LinkedSource.id).where(
                        LinkedSource.tenant_pk == tenant_pk,
                        LinkedSource.status.in_(
                            [SourceStatus.ACTIVE, SourceStatus.PENDING_CONSENT, SourceStatus.ERROR]
                        ),
                    )
                )
            ).all()
        )
        tenant_id = tenant.tenant_id
        await session.commit()

    if tenant_id:
        from app.graph.tokens import App

        await services.tokens.invalidate(App.SOURCE, tenant_id)
    for source_id in source_ids:
        await purge_source(services, source_id, final_status=SourceStatus.REVOKED, reason=reason)
    return f"revoked sources={len(source_ids)}"


async def revoke_tenant_by_entra_id(services: Services, tenant_id: str, reason: str) -> str:
    async with services.sessionmaker() as session:
        tenant_pk = await session.scalar(select(Tenant.id).where(Tenant.tenant_id == tenant_id))
    if tenant_pk is None:
        return "unknown-tenant"
    return await revoke_tenant(services, tenant_pk, reason)


async def activate_tenant_sources(services: Services, tenant_pk: uuid.UUID) -> int:
    """After admin consent, start every link that was waiting on it."""
    async with services.sessionmaker() as session:
        sources = (
            await session.scalars(
                select(LinkedSource).where(
                    LinkedSource.tenant_pk == tenant_pk,
                    LinkedSource.status.in_([SourceStatus.PENDING_CONSENT, SourceStatus.REVOKED]),
                )
            )
        ).all()
        for source in sources:
            source.status = SourceStatus.ACTIVE
            source.last_error = None
        ids = [s.id for s in sources]
        await session.commit()
    for source_id in ids:
        await services.queue.enqueue("setup_source", str(source_id))
    return len(ids)


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
                    services, source.tenant.tenant_id, source.subscription.graph_subscription_id
                )
        await session.commit()

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
