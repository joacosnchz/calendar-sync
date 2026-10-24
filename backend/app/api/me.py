"""End-user API: profile, linked sources with health (FR-13), unified calendar view."""

import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api import schemas
from app.api.deps import csrf_protect, current_user, get_services, get_session
from app.audit import audit
from app.models import LinkedSource, SourceEvent, SourceStatus, Tenant, User
from app.queue import sync_key
from app.security import SESSION_COOKIE
from app.services import Services
from app.sync.events import busiest
from app.sync.source_sync import SyncMode

router = APIRouter(prefix="/api", tags=["me"], dependencies=[Depends(csrf_protect)])


def source_out(source: LinkedSource, event_count: int) -> schemas.SourceOut:
    sub = source.subscription
    return schemas.SourceOut(
        id=source.id,
        label=source.label,
        source_email=source.source_email,
        status=source.status,
        tenant=schemas.TenantRef(
            id=source.tenant.id,
            display_name=source.tenant.display_name,
            tenant_id=source.tenant.tenant_id,
        ),
        last_synced_at=source.last_synced_at,
        last_full_sync_at=source.last_full_sync_at,
        last_notification_at=source.last_notification_at,
        last_error=source.last_error,
        last_error_at=source.last_error_at,
        subscription=schemas.SubscriptionHealth.model_validate(sub, from_attributes=True) if sub else None,
        event_count=event_count,
        reauth_url=f"/auth/reauth/{source.id}" if source.status == SourceStatus.REAUTH_REQUIRED else None,
        reauth_required_at=source.reauth_required_at,
        created_at=source.created_at,
    )


async def _owned_source(session: AsyncSession, user: User, source_id: uuid.UUID) -> LinkedSource:
    source = await session.get(
        LinkedSource,
        source_id,
        options=[selectinload(LinkedSource.tenant), selectinload(LinkedSource.subscription)],
    )
    # Isolation: a user can only ever see or act on their own links.
    if source is None or source.user_id != user.id:
        raise HTTPException(404, "Source not found")
    return source


@router.get("/me", response_model=schemas.MeOut)
async def me(user: User = Depends(current_user)) -> schemas.MeOut:
    return schemas.MeOut(
        id=user.id, email=user.email, display_name=user.display_name, is_operator=user.is_operator
    )


@router.delete("/me", status_code=202)
async def delete_account(
    response: Response,
    user: User = Depends(current_user),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> dict:
    audit(session, "user.deletion_requested", "user", user.id, actor=user)
    await session.commit()
    await services.queue.enqueue("delete_user", str(user.id))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "deleting"}


@router.get("/sources", response_model=list[schemas.SourceOut])
async def list_sources(
    user: User = Depends(current_user),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> list[schemas.SourceOut]:
    sources = (
        await session.scalars(
            select(LinkedSource)
            .where(LinkedSource.user_id == user.id)
            .options(selectinload(LinkedSource.tenant), selectinload(LinkedSource.subscription))
            .order_by(LinkedSource.created_at)
        )
    ).all()
    counts = dict(
        (
            await session.execute(
                select(SourceEvent.source_id, func.count())
                .where(SourceEvent.source_id.in_([s.id for s in sources]))
                .group_by(SourceEvent.source_id)
            )
        ).all()
    )
    return [source_out(s, counts.get(s.id, 0)) for s in sources]


@router.patch("/sources/{source_id}", response_model=schemas.SourceOut)
async def update_source(
    source_id: uuid.UUID,
    body: schemas.SourceUpdate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> schemas.SourceOut:
    source = await _owned_source(session, user, source_id)
    source.label = body.label
    await session.commit()
    # Labels are applied when the calendar is read, so there's nothing else to update.
    count = await session.scalar(
        select(func.count()).select_from(SourceEvent).where(SourceEvent.source_id == source.id)
    )
    return source_out(source, count or 0)


@router.delete("/sources/{source_id}", status_code=202)
async def unlink_source(
    source_id: uuid.UUID,
    user: User = Depends(current_user),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> dict:
    source = await _owned_source(session, user, source_id)
    source.status = SourceStatus.REMOVING
    audit(
        session, "source.unlink_requested", "linked_source", source.id, actor=user, email=source.source_email
    )
    await session.commit()
    await services.queue.enqueue("remove_source", str(source.id), "unlinked by user")
    return {"status": "removing"}


@router.post("/sources/{source_id}/resync", status_code=202)
async def resync_source(
    source_id: uuid.UUID,
    user: User = Depends(current_user),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> dict:
    source = await _owned_source(session, user, source_id)
    if source.status != SourceStatus.ACTIVE:
        raise HTTPException(409, f"Source is {source.status}")
    await services.queue.enqueue_coalesced(
        sync_key(source.id, SyncMode.FULL), "sync_source", str(source.id), SyncMode.FULL.value
    )
    return {"status": "queued"}


@router.get("/calendar", response_model=list[schemas.CalendarEventOut])
async def unified_calendar(
    start: datetime = Query(...),
    end: datetime = Query(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[schemas.CalendarEventOut]:
    """The unified calendar: every visible source's stored events, one entry per meeting
    (copies in several sources share a dedup key, FR-14)."""
    if end <= start or end - start > timedelta(days=62):
        raise HTTPException(400, "Range must be positive and at most 62 days")
    rows = (
        await session.execute(
            select(SourceEvent, LinkedSource.label, Tenant.copy_details)
            .join(LinkedSource, LinkedSource.id == SourceEvent.source_id)
            .join(Tenant, Tenant.id == LinkedSource.tenant_pk)
            .where(
                LinkedSource.user_id == user.id,
                LinkedSource.status.in_(SourceStatus.VISIBLE),
                SourceEvent.start_utc < end,
                SourceEvent.end_utc > start,
            )
        )
    ).all()
    groups: dict[str, list] = defaultdict(list)
    for ev, label, copy_details in rows:
        groups[ev.dedup_key].append((ev, label, copy_details))

    out = []
    for key, members in groups.items():
        canonical = max(members, key=lambda m: m[0].last_modified or m[0].start_utc)[0]
        copy_details = all(m[2] for m in members)
        out.append(
            schemas.CalendarEventOut(
                key=key,
                subject=(canonical.subject or "") if copy_details else "Busy",
                location=(canonical.location or "") if copy_details else "",
                start=canonical.start_utc,
                end=canonical.end_utc,
                is_all_day=canonical.is_all_day,
                show_as=busiest(m[0].show_as for m in members),
                labels=sorted({m[1] for m in members}),
                source_ids=sorted({m[0].source_id for m in members}),
            )
        )
    return sorted(out, key=lambda e: (e.start, e.end))
