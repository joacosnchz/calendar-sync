"""Reading source calendars: fetch-on-notify and calendarView delta queries."""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.graph.client import GraphGone, GraphNotFound
from app.metrics import DELTA_DRIFT
from app.models import LinkedSource, SourceEvent, SourceStatus, Subscription
from app.queue import reconcile_key, sync_key
from app.services import Services
from app.sync.events import normalize_event, to_row
from app.timeutil import sync_window, utcnow

log = logging.getLogger(__name__)

DELETE_CHUNK = 500


class SyncMode(StrEnum):
    INITIAL = "initial"  # first backfill after linking
    FULL = "full"  # operator- or system-requested re-baseline
    RECOVERY = "recovery"  # after `missed` / `subscriptionRemoved`
    SCHEDULED = "scheduled"  # periodic safety net


@dataclass
class SyncResult:
    changed_keys: set[str] = field(default_factory=set)
    rebaseline: bool = False
    pages: int = 0
    skipped: str | None = None


# --- storage -------------------------------------------------------------------------


async def upsert_event(
    session: AsyncSession, source_id: uuid.UUID, raw: dict[str, Any], *, authoritative: bool
) -> set[str]:
    """Store one event. Returns the dedup keys whose mirror may need to change.

    Notification path: only overwrite when Graph's lastModifiedDateTime is newer, which
    makes duplicate and out-of-order notifications harmless. Delta path is
    authoritative: any difference wins.
    """
    event = normalize_event(raw)
    row = to_row(source_id, event)
    stmt = pg_insert(SourceEvent).values(id=uuid.uuid4(), **row)
    if authoritative:
        condition = or_(
            SourceEvent.last_modified.is_distinct_from(stmt.excluded.last_modified),
            SourceEvent.start_utc != stmt.excluded.start_utc,
            SourceEvent.end_utc != stmt.excluded.end_utc,
        )
    else:
        condition = or_(
            SourceEvent.last_modified.is_(None), SourceEvent.last_modified < stmt.excluded.last_modified
        )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SourceEvent.source_id, SourceEvent.graph_event_id],
        set_={
            **{k: stmt.excluded[k] for k in row if k not in ("source_id", "graph_event_id")},
            "updated_at": func.now(),
        },
        where=condition,
    ).returning(SourceEvent.dedup_key)
    return set((await session.scalars(stmt)).all())


async def delete_events(session: AsyncSession, source_id: uuid.UUID, graph_ids: list[str]) -> set[str]:
    """Delete events (and occurrences of a deleted series master). Returns affected keys."""
    keys: set[str] = set()
    for i in range(0, len(graph_ids), DELETE_CHUNK):
        chunk = graph_ids[i : i + DELETE_CHUNK]
        result = await session.scalars(
            delete(SourceEvent)
            .where(
                SourceEvent.source_id == source_id,
                or_(SourceEvent.graph_event_id.in_(chunk), SourceEvent.series_master_id.in_(chunk)),
            )
            .returning(SourceEvent.dedup_key)
        )
        keys |= set(result.all())
    return keys


async def apply_event(
    session: AsyncSession,
    source_id: uuid.UUID,
    raw: dict[str, Any],
    window: tuple[datetime, datetime],
    *,
    authoritative: bool,
) -> tuple[set[str], bool]:
    """Returns (changed keys, needs_delta). Series masters aren't mirrored directly: their
    occurrences come from calendarView, so a change to one triggers a delta instead."""
    event = normalize_event(raw)
    if event.event_type == "seriesMaster":
        return set(), True
    if not event.is_mirrorable() or not event.overlaps(*window):
        return await delete_events(session, source_id, [event.graph_event_id]), False
    return await upsert_event(session, source_id, raw, authoritative=authoritative), False


async def enqueue_reconciles(services: Services, user_id: uuid.UUID, keys: set[str]) -> None:
    for key in keys:
        await services.queue.enqueue_coalesced(
            reconcile_key(user_id, key), "reconcile_group", str(user_id), key
        )


def current_window(services: Services) -> tuple[datetime, datetime]:
    s = services.settings
    return sync_window(utcnow(), s.sync_window_past_days, s.sync_window_future_days)


# --- fetch-on-notify -----------------------------------------------------------------


async def source_for_subscription(services: Services, graph_subscription_id: str) -> uuid.UUID | None:
    async with services.sessionmaker() as session:
        return await session.scalar(
            select(Subscription.source_id).where(Subscription.graph_subscription_id == graph_subscription_id)
        )


async def process_notification(services: Services, graph_subscription_id: str, resource_id: str) -> str:
    async with services.sessionmaker() as session:
        sub = await session.scalar(
            select(Subscription)
            .where(Subscription.graph_subscription_id == graph_subscription_id)
            .options(selectinload(Subscription.source).selectinload(LinkedSource.tenant))
        )
        if sub is None:
            return "unknown-subscription"
        source = sub.source
        source.last_notification_at = utcnow()
        if source.status != SourceStatus.ACTIVE or not source.tenant.tenant_id:
            await session.commit()
            return "inactive"

        graph = services.graph.source(source.id, source.tenant.tenant_id)
        needs_delta = False
        try:
            raw = await graph.get_event(source.source_user_id, resource_id)
        except GraphNotFound:
            keys = await delete_events(session, source.id, [resource_id])
            # Unknown id: most likely a series master (we store occurrences). Let delta
            # work out which occurrences went away.
            needs_delta = not keys
        else:
            keys, needs_delta = await apply_event(
                session, source.id, raw, current_window(services), authoritative=False
            )
        await session.commit()

    await enqueue_reconciles(services, source.user_id, keys)
    if needs_delta:
        await services.queue.enqueue_coalesced(
            sync_key(source.id, SyncMode.RECOVERY), "sync_source", str(source.id), SyncMode.RECOVERY.value
        )
    return f"changed={len(keys)} delta={needs_delta}"


# --- delta ---------------------------------------------------------------------------


async def sync_source(services: Services, source_id: uuid.UUID, mode: SyncMode) -> SyncResult:
    settings = services.settings
    async with services.sessionmaker() as session:
        source = await session.get(LinkedSource, source_id, options=[selectinload(LinkedSource.tenant)])
        if source is None or source.status != SourceStatus.ACTIVE or not source.tenant.tenant_id:
            return SyncResult(skipped="inactive")

        now = utcnow()
        baseline_age_limit = now - timedelta(hours=settings.delta_rebaseline_hours)
        rebaseline = (
            mode in (SyncMode.INITIAL, SyncMode.FULL)
            or not source.delta_link
            or source.last_full_sync_at is None
            or source.last_full_sync_at < baseline_age_limit
        )
        try:
            result = await _run_delta(services, session, source, rebaseline)
        except GraphGone:
            if rebaseline:
                raise
            # Raised while fetching a page, after the previous page was committed, so the
            # session holds no pending changes here.
            log.info("delta token expired for source %s; re-baselining", source.id)
            result = await _run_delta(services, session, source, True)

        if mode in (SyncMode.RECOVERY, SyncMode.SCHEDULED) and result.changed_keys:
            DELTA_DRIFT.labels(source.tenant.tenant_id).inc(len(result.changed_keys))
            log.warning("delta drift source=%s mode=%s changes=%d", source.id, mode, len(result.changed_keys))
        user_id = source.user_id

    await enqueue_reconciles(services, user_id, result.changed_keys)
    return result


async def _run_delta(
    services: Services, session: AsyncSession, source: LinkedSource, rebaseline: bool
) -> SyncResult:
    assert source.tenant.tenant_id
    graph = services.graph.source(source.id, source.tenant.tenant_id)
    result = SyncResult(rebaseline=rebaseline)
    if rebaseline:
        window = current_window(services)
        url: str | None = graph.delta_start_url(source.source_user_id, source.calendar_id, *window)
    else:
        assert source.delta_window_start and source.delta_window_end
        window = (source.delta_window_start, source.delta_window_end)
        url = source.delta_link

    seen: set[str] = set()
    delta_link: str | None = None
    while url:
        page = await graph.delta_page(url)
        result.pages += 1
        for item in page.get("value", []):
            if "@removed" in item:
                result.changed_keys |= await delete_events(session, source.id, [item["id"]])
                continue
            seen.add(item["id"])
            keys, _ = await apply_event(session, source.id, item, window, authoritative=True)
            result.changed_keys |= keys
        await session.commit()
        url = page.get("@odata.nextLink")
        delta_link = page.get("@odata.deltaLink") or delta_link

    if rebaseline:
        # Delta is the source of truth: anything stored that a full pass didn't return is gone.
        existing = set(
            (
                await session.scalars(
                    select(SourceEvent.graph_event_id).where(SourceEvent.source_id == source.id)
                )
            ).all()
        )
        result.changed_keys |= await delete_events(session, source.id, sorted(existing - seen))

    now = utcnow()
    source.delta_link = delta_link
    source.delta_window_start, source.delta_window_end = window
    source.last_synced_at = now
    if rebaseline:
        source.last_full_sync_at = now
    source.last_error = None
    await session.commit()
    return result
