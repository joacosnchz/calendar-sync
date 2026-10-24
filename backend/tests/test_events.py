"""Unit tests: event normalisation, time zones and combining copies of a meeting (no I/O)."""

import uuid
from datetime import UTC, datetime

from app.sync.events import busiest, dedup_key_for, normalize_event, parse_graph_datetime, resolve_tz, to_row


def raw_event(**overrides):
    event = {
        "id": "AAMkEvent1",
        "iCalUId": "ICAL-1",
        "type": "singleInstance",
        "subject": "Quarterly review",
        "start": {"dateTime": "2026-11-02T17:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-11-02T18:30:00.0000000", "timeZone": "UTC"},
        "originalStartTimeZone": "Pacific Standard Time",
        "originalEndTimeZone": "Pacific Standard Time",
        "isAllDay": False,
        "showAs": "busy",
        "sensitivity": "normal",
        "isCancelled": False,
        "location": {"displayName": "Room 1"},
        "lastModifiedDateTime": "2026-10-20T08:00:00.1234567Z",
        "attendees": [{"emailAddress": {"address": "someone@contoso.example"}}],
    }
    event.update(overrides)
    return event


class TestTimeZones:
    def test_resolves_windows_and_iana_names(self):
        assert resolve_tz("Pacific Standard Time").key == "America/Los_Angeles"
        assert resolve_tz("W. Europe Standard Time").key == "Europe/Berlin"
        assert resolve_tz("Europe/London").key == "Europe/London"
        assert resolve_tz("tzone://Microsoft/Custom") is None
        assert resolve_tz(None) is None

    def test_parses_seven_fractional_digits_as_utc(self):
        dt = parse_graph_datetime({"dateTime": "2026-11-02T17:00:00.1234567", "timeZone": "UTC"})
        assert dt == datetime(2026, 11, 2, 17, 0, 0, 123456, tzinfo=UTC)

    def test_converts_non_utc_input(self):
        dt = parse_graph_datetime({"dateTime": "2026-07-01T09:00:00", "timeZone": "Pacific Standard Time"})
        assert dt == datetime(2026, 7, 1, 16, 0, tzinfo=UTC)  # PDT is UTC-7


class TestNormalize:
    def test_keeps_only_needed_fields(self):
        """FR-9: attendees and body are never read into storage."""
        ev = normalize_event(raw_event(body={"contentType": "html", "content": "<p>Agenda</p>"}))
        assert ev.subject == "Quarterly review"
        assert ev.location == "Room 1"
        assert ev.start_utc == datetime(2026, 11, 2, 17, tzinfo=UTC)
        assert ev.start_tz == "Pacific Standard Time"
        assert ev.should_store()
        assert not hasattr(ev, "attendees") and not hasattr(ev, "body")
        assert not {"attendees", "body"} & set(to_row(uuid.uuid4(), ev))

    def test_all_day_dates_are_floating(self):
        ev = normalize_event(
            raw_event(
                isAllDay=True,
                start={"dateTime": "2026-11-03T00:00:00.0000000", "timeZone": "Pacific Standard Time"},
                end={"dateTime": "2026-11-04T00:00:00.0000000", "timeZone": "Pacific Standard Time"},
            )
        )
        assert ev.start_utc == datetime(2026, 11, 3, tzinfo=UTC)
        assert ev.end_utc == datetime(2026, 11, 4, tzinfo=UTC)

    def test_cancelled_and_series_masters_are_not_stored(self):
        assert not normalize_event(raw_event(isCancelled=True)).should_store()
        assert not normalize_event(raw_event(type="seriesMaster")).should_store()
        assert normalize_event(raw_event(type="occurrence")).should_store()
        assert normalize_event(raw_event(type="exception")).should_store()

    def test_dedup_key_prefers_ical_uid(self):
        src = uuid.uuid4()
        assert dedup_key_for(src, normalize_event(raw_event())) == "ical:ICAL-1"
        assert dedup_key_for(src, normalize_event(raw_event(iCalUId=None))) == f"src:{src}:AAMkEvent1"


class TestBusiest:
    """FR-10: when one meeting is in several sources, the busiest show-as wins."""

    def test_busiest_status_wins(self):
        assert busiest(["tentative", "busy"]) == "busy"
        assert busiest(["busy", "oof", "free"]) == "oof"
        assert busiest(["free", "workingElsewhere"]) == "workingElsewhere"
        assert busiest(["free"]) == "free"

    def test_unknown_values_count_as_busy(self):
        assert busiest(["tentative", "somethingNew"]) == "somethingNew"
        assert busiest(["oof", "somethingNew"]) == "oof"
