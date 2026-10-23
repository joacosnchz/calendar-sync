"""Building mirrored events and reconciling them into primary calendars.

A *group* is every source event of one user that shares a dedup key. Each group maps to
at most one mirrored event. Reconciling a group is idempotent: it reads the current
stored source state and makes the primary calendar match it.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select

from app.config import Settings
from app.graph.client import GraphNotFound
from app.metrics import MIRROR_WRITES
from app.models import LinkedSource, MirrorMapping, PrimaryStatus, SourceEvent, SourceStatus, Tenant, User
from app.queue import reconcile_key
from app.services import Services
from app.sync.events import resolve_tz
from app.timeutil import utcnow

log = logging.getLogger(__name__)

# Named MAPI property stamped on every mirror so operators can trace (or clean up) them.
MIRROR_PROPERTY_ID = "String {8a3c5f2e-6b1d-4f7a-9c0e-2d4b6a8e1f37} Name CalendarSyncKey"

SHOW_AS_RANK = {"free": 0, "unknown": 0, "workingElsewhere": 1, "tentative": 2, "busy": 3, "oof": 4}


@dataclass(frozen=True)
class MirrorInput:
    subject: str
    location: str
    start_utc: datetime
    end_utc: datetime
    start_tz: str | None
    end_tz: str | None
    is_all_day: bool
    show_as: str
    sensitivity: str
    last_modified: datetime | None
    label: str
    copy_details: bool


def graph_time(dt_utc: datetime, tz_name: str | None, all_day: bool) -> dict[str, str]:
    """Write times in the event's original time zone when we can resolve it."""
    tz = resolve_tz(tz_name)
    zone_name = tz_name if tz and tz_name else "UTC"
    if all_day:
        return {"dateTime": dt_utc.strftime("%Y-%m-%dT00:00:00"), "timeZone": zone_name}
    local = dt_utc.astimezone(tz) if tz else dt_utc
    return {"dateTime": local.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": zone_name}


def build_mirror_payload(dedup_key: str, inputs: list[MirrorInput], settings: Settings) -> dict[str, Any]:
    if not inputs:
        raise ValueError("A mirror needs at least one source event")
    canonical = max(inputs, key=lambda i: (i.last_modified is not None, i.last_modified or datetime.min))
    labels = sorted({i.label for i in inputs})
    copy_details = all(i.copy_details for i in inputs)
    show_as = max((i.show_as for i in inputs), key=lambda s: SHOW_AS_RANK.get(s, 3))
    sensitivity = "private" if any(i.sensitivity in ("private", "confidential") for i in inputs) else "normal"

    subject = canonical.subject if copy_details else "Busy"
    if settings.mirror_label_in_subject:
        subject = f"[{' · '.join(labels)}] {subject}".strip()
    location = canonical.location if copy_details else ""

    # FR-9: mirrors never carry attendees, so Exchange never sends invitations or responses.
    return {
        "subject": subject,
        "start": graph_time(canonical.start_utc, canonical.start_tz, canonical.is_all_day),
        "end": graph_time(canonical.end_utc, canonical.end_tz or canonical.start_tz, canonical.is_all_day),
        "isAllDay": canonical.is_all_day,
        "showAs": show_as,
        "sensitivity": sensitivity,
        "location": {"displayName": location},
        "categories": labels,
        "body": {
            "contentType": "text",
            "content": f"Mirrored by Calendar Sync from {', '.join(labels)}. "
            "Changes made here are not copied back to the source calendar.",
        },
        "isReminderOn": False,
        "responseRequested": False,
        "allowNewTimeProposals": False,
        "singleValueExtendedProperties": [{"id": MIRROR_PROPERTY_ID, "value": dedup_key}],
    }


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _group_inputs(session, user_id: uuid.UUID, dedup_key: str) -> list[MirrorInput]:
    rows = (
        await session.execute(
            select(SourceEvent, LinkedSource.label, Tenant.copy_details)
            .join(LinkedSource, LinkedSource.id == SourceEvent.source_id)
            .join(Tenant, Tenant.id == LinkedSource.tenant_pk)
            .where(
                LinkedSource.user_id == user_id,
                LinkedSource.status == SourceStatus.ACTIVE,
                SourceEvent.dedup_key == dedup_key,
            )
        )
    ).all()
    return [
        MirrorInput(
            subject=ev.subject or "",
            location=ev.location or "",
            start_utc=ev.start_utc,
            end_utc=ev.end_utc,
            start_tz=ev.start_tz,
            end_tz=ev.end_tz,
            is_all_day=ev.is_all_day,
            show_as=ev.show_as,
            sensitivity=ev.sensitivity,
            last_modified=ev.last_modified,
            label=label,
            copy_details=copy_details,
        )
        for ev, label, copy_details in rows
    ]


async def reconcile_group(
    services: Services, user_id: uuid.UUID, dedup_key: str, *, force: bool = False
) -> str:
    """Make the user's primary calendar match the stored source state for one group."""
    lock = services.redis.lock(f"lock:{reconcile_key(user_id, dedup_key)}", timeout=180, blocking_timeout=90)
    async with lock, services.sessionmaker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return "no-user"
        inputs = await _group_inputs(session, user_id, dedup_key)
        mapping = await session.scalar(
            select(MirrorMapping).where(
                MirrorMapping.user_id == user_id, MirrorMapping.dedup_key == dedup_key
            )
        )
        primary = services.graph.primary()
        now = utcnow()

        if not inputs:
            if mapping is None:
                return "noop"
            if mapping.graph_event_id and mapping.primary_mailbox:
                try:
                    await primary.delete_event(mapping.primary_mailbox, mapping.graph_event_id)
                    MIRROR_WRITES.labels("delete").inc()
                except GraphNotFound:
                    pass
            await session.delete(mapping)
            await session.commit()
            return "deleted"

        if (
            user.primary_status != PrimaryStatus.READY
            or not user.primary_mailbox
            or not user.primary_calendar_id
        ):
            return "primary-not-ready"

        payload = build_mirror_payload(dedup_key, inputs, services.settings)
        digest = content_hash(payload)
        target = (user.primary_mailbox, user.primary_calendar_id)

        if mapping is None:
            mapping = MirrorMapping(user_id=user_id, dedup_key=dedup_key)
            session.add(mapping)

        if mapping.graph_event_id and (mapping.primary_mailbox, mapping.primary_calendar_id) != target:
            # The user's primary calendar moved; remove the mirror from the old one.
            try:
                await primary.delete_event(mapping.primary_mailbox or target[0], mapping.graph_event_id)
            except GraphNotFound:
                pass
            mapping.graph_event_id = None

        if mapping.graph_event_id and mapping.content_hash == digest and not force:
            mapping.reconciled_at = now
            await session.commit()
            return "unchanged"

        op = "updated"
        if mapping.graph_event_id:
            try:
                await primary.update_event(target[0], mapping.graph_event_id, payload)
            except GraphNotFound:
                mapping.graph_event_id = None

        if not mapping.graph_event_id:
            if not mapping.pending_transaction_id:
                mapping.pending_transaction_id = str(uuid.uuid4())
                await session.commit()  # persist before the POST so a retry reuses it
            created = await primary.create_event(
                target[0], target[1], {**payload, "transactionId": mapping.pending_transaction_id}
            )
            mapping.graph_event_id = created["id"]
            mapping.pending_transaction_id = None
            op = "created"

        MIRROR_WRITES.labels(op).inc()
        mapping.primary_mailbox, mapping.primary_calendar_id = target
        mapping.content_hash = digest
        mapping.last_written_at = now
        mapping.reconciled_at = now
        await session.commit()
        return op


async def stale_dedup_keys(session, user_id: uuid.UUID, limit: int = 5000) -> set[str]:
    """Groups whose mirror may not match stored state: never mirrored, changed since the
    last reconcile, or mirrored with no remaining active source event."""
    active = and_(
        LinkedSource.id == SourceEvent.source_id,
        LinkedSource.user_id == user_id,
        LinkedSource.status == SourceStatus.ACTIVE,
    )
    latest = (
        select(SourceEvent.dedup_key, func.max(SourceEvent.updated_at).label("changed"))
        .join(LinkedSource, active)
        .group_by(SourceEvent.dedup_key)
        .subquery()
    )
    needs_write = await session.scalars(
        select(latest.c.dedup_key)
        .outerjoin(
            MirrorMapping,
            and_(MirrorMapping.user_id == user_id, MirrorMapping.dedup_key == latest.c.dedup_key),
        )
        .where(
            or_(
                MirrorMapping.id.is_(None),
                MirrorMapping.graph_event_id.is_(None),
                MirrorMapping.reconciled_at.is_(None),
                MirrorMapping.reconciled_at < latest.c.changed,
            )
        )
        .limit(limit)
    )
    orphaned = await session.scalars(
        select(MirrorMapping.dedup_key)
        .where(MirrorMapping.user_id == user_id)
        .where(~select(latest.c.dedup_key).where(latest.c.dedup_key == MirrorMapping.dedup_key).exists())
        .limit(limit)
    )
    return set(needs_write) | set(orphaned)


async def all_dedup_keys(session, user_id: uuid.UUID) -> set[str]:
    live = await session.scalars(
        select(SourceEvent.dedup_key)
        .join(LinkedSource, LinkedSource.id == SourceEvent.source_id)
        .where(LinkedSource.user_id == user_id, LinkedSource.status == SourceStatus.ACTIVE)
        .distinct()
    )
    mapped = await session.scalars(select(MirrorMapping.dedup_key).where(MirrorMapping.user_id == user_id))
    return set(live) | set(mapped)
