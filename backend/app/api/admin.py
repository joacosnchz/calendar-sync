"""Operator API: customer tenants (FR-1), primary calendars (FR-2), sync health (FR-13).

Customer tenants are recorded automatically the first time a user links a mailbox from
one; there is no onboarding or admin consent step. Operators can rename them and set the
per-tenant privacy control.
"""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import schemas
from app.api.deps import csrf_protect, get_services, get_session, require_operator
from app.audit import audit
from app.models import (
    AuditLog,
    LinkedSource,
    MirrorMapping,
    PrimaryStatus,
    SourceEvent,
    SourceStatus,
    Subscription,
    SubscriptionStatus,
    SyncError,
    Tenant,
    User,
)
from app.queue import sync_key
from app.services import Services
from app.sync.source_sync import SyncMode, enqueue_reconciles
from app.timeutil import utcnow

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(csrf_protect)])


@router.get("/overview", response_model=schemas.OverviewOut)
async def overview(
    _: User = Depends(require_operator), session: AsyncSession = Depends(get_session)
) -> schemas.OverviewOut:
    now = utcnow()
    sources = dict(
        (await session.execute(select(LinkedSource.status, func.count()).group_by(LinkedSource.status))).all()
    )
    return schemas.OverviewOut(
        users=await session.scalar(select(func.count()).select_from(User)) or 0,
        tenants=await session.scalar(select(func.count()).select_from(Tenant)) or 0,
        sources=sources,
        subscriptions=await session.scalar(select(func.count()).select_from(Subscription)) or 0,
        subscriptions_expiring_24h=await session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.expires_at < now + timedelta(hours=24))
        )
        or 0,
        subscriptions_failing=await session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.status != SubscriptionStatus.ACTIVE)
        )
        or 0,
        errors_24h=await session.scalar(
            select(func.count())
            .select_from(SyncError)
            .where(SyncError.created_at > now - timedelta(hours=24))
        )
        or 0,
        mirrors=await session.scalar(
            select(func.count()).select_from(MirrorMapping).where(MirrorMapping.graph_event_id.is_not(None))
        )
        or 0,
    )


# --- tenants -------------------------------------------------------------------------


async def _tenant_out(session: AsyncSession, tenants: list[Tenant]) -> list[schemas.TenantOut]:
    ids = [t.id for t in tenants]
    since = utcnow() - timedelta(hours=24)
    stats = {
        row.tenant_pk: row
        for row in (
            await session.execute(
                select(
                    LinkedSource.tenant_pk,
                    func.count().label("total"),
                    func.count().filter(LinkedSource.status == SourceStatus.ACTIVE).label("active"),
                    func.count()
                    .filter(LinkedSource.status == SourceStatus.REAUTH_REQUIRED)
                    .label("reauth_required"),
                    func.max(LinkedSource.last_notification_at).label("last_notification"),
                    func.min(Subscription.expires_at).label("next_expiry"),
                )
                .outerjoin(Subscription, Subscription.source_id == LinkedSource.id)
                .where(LinkedSource.tenant_pk.in_(ids))
                .group_by(LinkedSource.tenant_pk)
            )
        ).all()
    }
    errors = dict(
        (
            await session.execute(
                select(SyncError.tenant_pk, func.count())
                .where(SyncError.tenant_pk.in_(ids), SyncError.created_at > since)
                .group_by(SyncError.tenant_pk)
            )
        ).all()
    )
    out = []
    for t in tenants:
        s = stats.get(t.id)
        out.append(
            schemas.TenantOut(
                id=t.id,
                display_name=t.display_name,
                domain=t.domain,
                tenant_id=t.tenant_id,
                copy_details=t.copy_details,
                last_error=t.last_error,
                created_at=t.created_at,
                source_count=s.total if s else 0,
                active_source_count=s.active if s else 0,
                reauth_required_count=s.reauth_required if s else 0,
                next_subscription_expiry=s.next_expiry if s else None,
                last_notification_at=s.last_notification if s else None,
                errors_24h=errors.get(t.id, 0),
            )
        )
    return out


@router.get("/tenants", response_model=list[schemas.TenantOut])
async def list_tenants(
    _: User = Depends(require_operator), session: AsyncSession = Depends(get_session)
) -> list[schemas.TenantOut]:
    tenants = list((await session.scalars(select(Tenant).order_by(Tenant.display_name))).all())
    return await _tenant_out(session, tenants)


@router.patch("/tenants/{tenant_pk}", response_model=schemas.TenantOut)
async def update_tenant(
    tenant_pk: uuid.UUID,
    body: schemas.TenantUpdate,
    operator: User = Depends(require_operator),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> schemas.TenantOut:
    tenant = await session.get(Tenant, tenant_pk)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    details_changed = body.copy_details is not None and body.copy_details != tenant.copy_details
    if body.display_name is not None:
        tenant.display_name = body.display_name
    if body.copy_details is not None:
        tenant.copy_details = body.copy_details
    audit(
        session, "tenant.updated", "tenant", tenant.id, actor=operator, **body.model_dump(exclude_none=True)
    )
    await session.commit()
    if details_changed:
        rows = (
            await session.execute(
                select(LinkedSource.user_id, SourceEvent.dedup_key)
                .join(SourceEvent, SourceEvent.source_id == LinkedSource.id)
                .where(LinkedSource.tenant_pk == tenant.id)
                .distinct()
            )
        ).all()
        by_user: dict[uuid.UUID, set[str]] = {}
        for user_id, key in rows:
            by_user.setdefault(user_id, set()).add(key)
        for user_id, keys in by_user.items():
            await enqueue_reconciles(services, user_id, keys)
    return (await _tenant_out(session, [tenant]))[0]


# --- users ---------------------------------------------------------------------------


@router.get("/users", response_model=list[schemas.UserAdminOut])
async def list_users(
    _: User = Depends(require_operator), session: AsyncSession = Depends(get_session)
) -> list[schemas.UserAdminOut]:
    users = (await session.scalars(select(User).order_by(User.created_at))).all()
    sources = dict(
        (
            await session.execute(select(LinkedSource.user_id, func.count()).group_by(LinkedSource.user_id))
        ).all()
    )
    mirrors = dict(
        (
            await session.execute(
                select(MirrorMapping.user_id, func.count())
                .where(MirrorMapping.graph_event_id.is_not(None))
                .group_by(MirrorMapping.user_id)
            )
        ).all()
    )
    return [
        schemas.UserAdminOut(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            is_operator=u.is_operator,
            primary_status=u.primary_status,
            primary_mailbox=u.primary_mailbox,
            primary_error=u.primary_error,
            source_count=sources.get(u.id, 0),
            mirror_count=mirrors.get(u.id, 0),
            created_at=u.created_at,
        )
        for u in users
    ]


@router.patch("/users/{user_id}", status_code=202)
async def update_user(
    user_id: uuid.UUID,
    body: schemas.UserAdminUpdate,
    operator: User = Depends(require_operator),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    provision = False
    if body.is_operator is not None:
        if user.id == operator.id and not body.is_operator:
            raise HTTPException(400, "You can't remove your own operator access")
        user.is_operator = body.is_operator
    if body.primary_mailbox is not None:
        if services.settings.primary_calendar_mode != "user_mailbox":
            raise HTTPException(400, "Primary mailboxes are assigned automatically in shared_mailbox mode")
        mailbox = body.primary_mailbox.strip().lower()
        if mailbox != (user.primary_mailbox or ""):
            user.primary_mailbox = mailbox
            user.primary_calendar_id = None
            user.primary_status = PrimaryStatus.PROVISIONING
            provision = True
    audit(session, "user.updated", "user", user.id, actor=operator, **body.model_dump(exclude_none=True))
    await session.commit()
    if provision:
        await services.queue.enqueue("provision_primary", str(user.id))
    return {"status": "provisioning" if provision else "updated"}


@router.post("/users/{user_id}/provision", status_code=202)
async def provision_user(
    user_id: uuid.UUID,
    operator: User = Depends(require_operator),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user.primary_status = PrimaryStatus.PROVISIONING
    user.primary_error = None
    audit(session, "primary.provision_requested", "user", user.id, actor=operator)
    await session.commit()
    await services.queue.enqueue("provision_primary", str(user.id))
    return {"status": "provisioning"}


@router.post("/users/{user_id}/rebuild", status_code=202)
async def rebuild_user(
    user_id: uuid.UUID,
    operator: User = Depends(require_operator),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if await session.get(User, user_id) is None:
        raise HTTPException(404, "User not found")
    audit(session, "mirrors.rebuild_requested", "user", user_id, actor=operator)
    await session.commit()
    await services.queue.enqueue("rebuild_user", str(user_id))
    return {"status": "queued"}


@router.post("/sources/{source_id}/resync", status_code=202)
async def resync_source(
    source_id: uuid.UUID,
    operator: User = Depends(require_operator),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> dict:
    source = await session.get(LinkedSource, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    audit(session, "source.resync_requested", "linked_source", source.id, actor=operator)
    await session.commit()
    await services.queue.enqueue_coalesced(
        sync_key(source.id, SyncMode.FULL), "sync_source", str(source.id), SyncMode.FULL.value
    )
    return {"status": "queued"}


# --- health --------------------------------------------------------------------------


@router.get("/subscriptions", response_model=list[schemas.SubscriptionAdminOut])
async def list_subscriptions(
    _: User = Depends(require_operator), session: AsyncSession = Depends(get_session)
) -> list[schemas.SubscriptionAdminOut]:
    rows = (
        await session.execute(
            select(Subscription, LinkedSource.source_email, User.email, Tenant.display_name)
            .join(LinkedSource, LinkedSource.id == Subscription.source_id)
            .join(User, User.id == LinkedSource.user_id)
            .join(Tenant, Tenant.id == LinkedSource.tenant_pk)
            .order_by(Subscription.expires_at)
        )
    ).all()
    return [
        schemas.SubscriptionAdminOut(
            id=sub.id,
            graph_subscription_id=sub.graph_subscription_id,
            source_id=sub.source_id,
            source_email=source_email,
            user_email=user_email,
            tenant_name=tenant_name,
            expires_at=sub.expires_at,
            renew_due_at=sub.renew_due_at,
            last_renewed_at=sub.last_renewed_at,
            status=sub.status,
            last_error=sub.last_error,
        )
        for sub, source_email, user_email, tenant_name in rows
    ]


@router.get("/errors", response_model=list[schemas.SyncErrorOut])
async def list_errors(
    limit: int = Query(100, le=500),
    tenant_pk: uuid.UUID | None = None,
    _: User = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> list[schemas.SyncErrorOut]:
    query = (
        select(SyncError, LinkedSource.source_email, User.email, Tenant.display_name)
        .outerjoin(LinkedSource, LinkedSource.id == SyncError.source_id)
        .outerjoin(User, User.id == SyncError.user_id)
        .outerjoin(Tenant, Tenant.id == SyncError.tenant_pk)
        .order_by(SyncError.created_at.desc())
        .limit(limit)
    )
    if tenant_pk:
        query = query.where(SyncError.tenant_pk == tenant_pk)
    return [
        schemas.SyncErrorOut(
            id=e.id,
            kind=e.kind,
            message=e.message,
            created_at=e.created_at,
            source_email=source_email,
            user_email=user_email,
            tenant_name=tenant_name,
        )
        for e, source_email, user_email, tenant_name in (await session.execute(query)).all()
    ]


@router.get("/audit", response_model=list[schemas.AuditOut])
async def list_audit(
    limit: int = Query(100, le=500),
    _: User = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> list[schemas.AuditOut]:
    rows = (await session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))).all()
    return [schemas.AuditOut.model_validate(r, from_attributes=True) for r in rows]
