"""Normalising Graph events into the minimal fields we store."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal.windows_tz import win_tz

MIRRORABLE_TYPES = {"singleInstance", "occurrence", "exception"}


@lru_cache(maxsize=512)
def resolve_tz(name: str | None) -> ZoneInfo | None:
    """Resolve an IANA or Windows time zone name (Graph returns either)."""
    if not name:
        return None
    for candidate in (name, win_tz.get(name)):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def parse_graph_datetime(value: dict[str, str]) -> datetime:
    """Parse Graph's dateTimeTimeZone (7 fractional digits) into an aware UTC datetime."""
    raw = value["dateTime"].rstrip("Z")
    if "." in raw:
        head, frac = raw.split(".", 1)
        raw = f"{head}.{frac[:6]}"
    naive = datetime.fromisoformat(raw)
    tz = resolve_tz(value.get("timeZone") or "UTC") or UTC
    return naive.replace(tzinfo=tz).astimezone(UTC)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return parse_graph_datetime({"dateTime": value, "timeZone": "UTC"})


@dataclass(frozen=True)
class NormalizedEvent:
    graph_event_id: str
    ical_uid: str | None
    event_type: str
    series_master_id: str | None
    subject: str
    location: str
    start_utc: datetime
    end_utc: datetime
    start_tz: str | None
    end_tz: str | None
    is_all_day: bool
    show_as: str
    sensitivity: str
    is_cancelled: bool
    last_modified: datetime | None

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start_utc < end and self.end_utc > start

    def is_mirrorable(self) -> bool:
        return self.event_type in MIRRORABLE_TYPES and not self.is_cancelled


def parse_all_day(value: dict[str, str]) -> datetime:
    """All-day events are floating dates: keep the calendar date, ignore the zone."""
    return datetime.fromisoformat(value["dateTime"][:10]).replace(tzinfo=UTC)


def normalize_event(raw: dict[str, Any]) -> NormalizedEvent:
    location = (raw.get("location") or {}).get("displayName") or ""
    is_all_day = bool(raw.get("isAllDay"))
    parse = parse_all_day if is_all_day else parse_graph_datetime
    return NormalizedEvent(
        graph_event_id=raw["id"],
        ical_uid=raw.get("iCalUId") or None,
        event_type=raw.get("type") or "singleInstance",
        series_master_id=raw.get("seriesMasterId"),
        subject=raw.get("subject") or "",
        location=location,
        start_utc=parse(raw["start"]),
        end_utc=parse(raw["end"]),
        start_tz=raw.get("originalStartTimeZone") or None,
        end_tz=raw.get("originalEndTimeZone") or None,
        is_all_day=is_all_day,
        show_as=raw.get("showAs") or "busy",
        sensitivity=raw.get("sensitivity") or "normal",
        is_cancelled=bool(raw.get("isCancelled")),
        last_modified=parse_iso(raw.get("lastModifiedDateTime")),
    )


def dedup_key_for(source_id: uuid.UUID, event: NormalizedEvent) -> str:
    # iCalUId is shared by every copy of a meeting (and is distinct per occurrence), so
    # one meeting that appears in two of a user's sources collapses to one mirror (FR-14).
    if event.ical_uid:
        return f"ical:{event.ical_uid}"
    return f"src:{source_id}:{event.graph_event_id}"


def to_row(source_id: uuid.UUID, event: NormalizedEvent) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "graph_event_id": event.graph_event_id,
        "ical_uid": event.ical_uid,
        "dedup_key": dedup_key_for(source_id, event),
        "event_type": event.event_type,
        "series_master_id": event.series_master_id,
        "subject": event.subject,
        "location": event.location,
        "start_utc": event.start_utc,
        "end_utc": event.end_utc,
        "start_tz": event.start_tz,
        "end_tz": event.end_tz,
        "is_all_day": event.is_all_day,
        "show_as": event.show_as,
        "sensitivity": event.sensitivity,
        "last_modified": event.last_modified,
    }
