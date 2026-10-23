"""Unit tests: event normalisation, time zones and mirror payloads (no I/O)."""

import uuid
from datetime import UTC, datetime

import pytest

from app.sync.events import dedup_key_for, normalize_event, parse_graph_datetime, resolve_tz
from app.sync.mirror import MirrorInput, build_mirror_payload, content_hash, graph_time
from tests.harness import make_settings


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


def mirror_input(**overrides):
    values = dict(
        subject="Quarterly review",
        location="Room 1",
        start_utc=datetime(2026, 11, 2, 17, tzinfo=UTC),
        end_utc=datetime(2026, 11, 2, 18, 30, tzinfo=UTC),
        start_tz="Pacific Standard Time",
        end_tz="Pacific Standard Time",
        is_all_day=False,
        show_as="busy",
        sensitivity="normal",
        last_modified=datetime(2026, 10, 20, tzinfo=UTC),
        label="Contoso",
        copy_details=True,
    )
    values.update(overrides)
    return MirrorInput(**values)


@pytest.fixture(scope="module")
def settings():
    return make_settings()


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

    def test_mirror_written_in_original_zone(self):
        # 17:00 UTC on 2 Nov 2026 is 09:00 PST (DST ended 1 Nov).
        assert graph_time(datetime(2026, 11, 2, 17, tzinfo=UTC), "Pacific Standard Time", False) == {
            "dateTime": "2026-11-02T09:00:00",
            "timeZone": "Pacific Standard Time",
        }

    def test_unknown_zone_falls_back_to_utc(self):
        assert graph_time(datetime(2026, 11, 2, 17, tzinfo=UTC), "tzone://Microsoft/Custom", False) == {
            "dateTime": "2026-11-02T17:00:00",
            "timeZone": "UTC",
        }

    def test_all_day_keeps_calendar_date(self):
        assert graph_time(datetime(2026, 11, 3, tzinfo=UTC), "Pacific Standard Time", True) == {
            "dateTime": "2026-11-03T00:00:00",
            "timeZone": "Pacific Standard Time",
        }


class TestNormalize:
    def test_keeps_only_needed_fields(self):
        ev = normalize_event(raw_event())
        assert ev.subject == "Quarterly review"
        assert ev.location == "Room 1"
        assert ev.start_utc == datetime(2026, 11, 2, 17, tzinfo=UTC)
        assert ev.start_tz == "Pacific Standard Time"
        assert ev.is_mirrorable()
        assert not hasattr(ev, "attendees")

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

    def test_cancelled_and_series_masters_are_not_mirrored(self):
        assert not normalize_event(raw_event(isCancelled=True)).is_mirrorable()
        assert not normalize_event(raw_event(type="seriesMaster")).is_mirrorable()
        assert normalize_event(raw_event(type="occurrence")).is_mirrorable()
        assert normalize_event(raw_event(type="exception")).is_mirrorable()

    def test_dedup_key_prefers_ical_uid(self):
        src = uuid.uuid4()
        assert dedup_key_for(src, normalize_event(raw_event())) == "ical:ICAL-1"
        assert dedup_key_for(src, normalize_event(raw_event(iCalUId=None))) == f"src:{src}:AAMkEvent1"


class TestMirrorPayload:
    def test_never_has_attendees(self, settings):
        """FR-9: mirrors never carry attendees, so no invitations or responses are sent."""
        payload = build_mirror_payload("ical:1", [mirror_input()], settings)
        assert "attendees" not in payload
        assert payload["responseRequested"] is False
        assert payload["isReminderOn"] is False

    def test_labels_source_and_keeps_show_as(self, settings):
        payload = build_mirror_payload("ical:1", [mirror_input(show_as="tentative")], settings)
        assert payload["subject"] == "[Contoso] Quarterly review"
        assert payload["categories"] == ["Contoso"]
        assert payload["showAs"] == "tentative"
        assert payload["start"] == {"dateTime": "2026-11-02T09:00:00", "timeZone": "Pacific Standard Time"}
        assert payload["singleValueExtendedProperties"][0]["value"] == "ical:1"

    def test_label_can_stay_out_of_subject(self):
        payload = build_mirror_payload("k", [mirror_input()], make_settings(mirror_label_in_subject=False))
        assert payload["subject"] == "Quarterly review"
        assert payload["categories"] == ["Contoso"]

    def test_duplicate_meeting_merges_labels_and_takes_busiest_status(self, settings):
        payload = build_mirror_payload(
            "ical:1",
            [
                mirror_input(label="Contoso", show_as="tentative"),
                mirror_input(
                    label="Fabrikam", show_as="busy", last_modified=datetime(2026, 10, 21, tzinfo=UTC)
                ),
            ],
            settings,
        )
        assert payload["subject"] == "[Contoso · Fabrikam] Quarterly review"
        assert payload["showAs"] == "busy"

    def test_newest_copy_wins(self, settings):
        payload = build_mirror_payload(
            "k",
            [
                mirror_input(subject="Old title", last_modified=datetime(2026, 10, 1, tzinfo=UTC)),
                mirror_input(subject="New title", last_modified=datetime(2026, 10, 2, tzinfo=UTC), label="B"),
            ],
            settings,
        )
        assert payload["subject"].endswith("New title")

    def test_privacy_mode_hides_details(self, settings):
        payload = build_mirror_payload("k", [mirror_input(copy_details=False)], settings)
        assert payload["subject"] == "[Contoso] Busy"
        assert payload["location"] == {"displayName": ""}

    def test_private_sensitivity_propagates(self, settings):
        payload = build_mirror_payload("k", [mirror_input(sensitivity="private")], settings)
        assert payload["sensitivity"] == "private"

    def test_hash_is_stable(self, settings):
        a = build_mirror_payload("k", [mirror_input()], settings)
        b = build_mirror_payload("k", [mirror_input()], settings)
        assert content_hash(a) == content_hash(b)
        c = build_mirror_payload("k", [mirror_input(subject="Changed")], settings)
        assert content_hash(a) != content_hash(c)
