"""Tests for the EventKit source.

We can't exercise the real EventKit framework in CI (Linux runners,
no Calendar.app). These tests cover the pieces that don't need the
framework: pure helpers, calendar-resolution logic with a fake store,
and the EventKitNotAvailable code path when PyObjC isn't importable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from webcal_mcp.config import CalendarConfig
from webcal_mcp.eventkit import EventKitNotAvailable, EventKitSource, _to_event

try:
    import EventKit  # type: ignore[import-not-found]  # noqa: F401

    HAS_PYOBJC = True
except ImportError:
    HAS_PYOBJC = False


def _ek_event(**overrides: Any) -> SimpleNamespace:
    """Build a minimal duck-typed stand-in for an EKEvent."""
    base = {
        "title": lambda: "Standup",
        "startDate": lambda: SimpleNamespace(timeIntervalSince1970=lambda: 1_780_000_000.0),
        "endDate": lambda: SimpleNamespace(timeIntervalSince1970=lambda: 1_780_003_600.0),
        "isAllDay": lambda: False,
        "notes": lambda: "Daily sync",
        "location": lambda: "Zoom",
        "organizer": lambda: None,
        "attendees": lambda: None,
        "status": lambda: 1,
        "URL": lambda: None,
        "calendarItemExternalIdentifier": lambda: "evt-1@test",
        "calendarItemIdentifier": lambda: "internal-id",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_to_event_basic() -> None:
    event = _to_event(_ek_event())
    assert event.uid == "evt-1@test"
    assert event.summary == "Standup"
    assert event.location == "Zoom"
    assert event.status == "CONFIRMED"
    assert event.all_day is False
    assert event.start == datetime.fromtimestamp(1_780_000_000.0, tz=timezone.utc)


def test_to_event_attendees_and_organizer() -> None:
    organizer = SimpleNamespace(name=lambda: "Alice", URL=lambda: "mailto:a@x")
    attendees = [
        SimpleNamespace(name=lambda: "Bob", URL=lambda: "mailto:b@x"),
        SimpleNamespace(name=lambda: None, URL=lambda: "mailto:c@x"),
    ]
    event = _to_event(
        _ek_event(organizer=lambda: organizer, attendees=lambda: attendees)
    )
    assert event.organizer == "Alice"
    assert event.attendees == ("Bob", "mailto:c@x")


def test_to_event_falls_back_to_internal_id_when_external_missing() -> None:
    event = _to_event(
        _ek_event(calendarItemExternalIdentifier=lambda: None)
    )
    assert event.uid == "internal-id"


def _fake_cal(title: str, uuid: str) -> SimpleNamespace:
    return SimpleNamespace(title=lambda: title, calendarIdentifier=lambda: uuid)


def _fake_store(cals: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(calendarsForEntityType_=lambda _t: cals)


def _source(identifier: str) -> EventKitSource:
    return EventKitSource(
        CalendarConfig(name="cfg-name", source="eventkit", identifier=identifier)
    )


def test_find_calendar_by_uuid() -> None:
    cals = [_fake_cal("Personal", "uuid-A"), _fake_cal("Work", "uuid-B")]
    src = _source("uuid-B")
    assert src._find_calendar(_fake_store(cals), object()).title() == "Work"


def test_find_calendar_by_exact_title() -> None:
    cals = [_fake_cal("Personal", "uuid-A"), _fake_cal("Work", "uuid-B")]
    src = _source("Work")
    assert src._find_calendar(_fake_store(cals), object()).title() == "Work"


def test_find_calendar_case_insensitive_fallback() -> None:
    cals = [_fake_cal("Personal", "uuid-A")]
    src = _source("personal")
    assert src._find_calendar(_fake_store(cals), object()).title() == "Personal"


def test_find_calendar_ambiguous_title_rejected() -> None:
    cals = [_fake_cal("Personal", "uuid-A"), _fake_cal("Personal", "uuid-B")]
    src = _source("Personal")
    with pytest.raises(EventKitNotAvailable, match="ambiguous|matches 2"):
        src._find_calendar(_fake_store(cals), object())


def test_find_calendar_missing_lists_available() -> None:
    cals = [_fake_cal("Personal", "uuid-A"), _fake_cal("Work", "uuid-B")]
    src = _source("Nope")
    with pytest.raises(EventKitNotAvailable, match="Personal.*Work|Work.*Personal"):
        src._find_calendar(_fake_store(cals), object())


def test_identifier_defaults_to_name() -> None:
    src = EventKitSource(CalendarConfig(name="Calendars", source="eventkit"))
    cals = [_fake_cal("Calendars", "uuid-X")]
    assert src._find_calendar(_fake_store(cals), object()).title() == "Calendars"


@pytest.mark.skipif(
    HAS_PYOBJC, reason="PyObjC is installed; can't exercise the missing-import path"
)
def test_eventkit_not_available_when_pyobjc_missing() -> None:
    """When PyObjC isn't installed, _import_eventkit raises EventKitNotAvailable."""
    from webcal_mcp.eventkit import _import_eventkit

    with pytest.raises(EventKitNotAvailable):
        _import_eventkit()
