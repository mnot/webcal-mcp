"""Tests for ICS parsing, normalization, recurrence expansion, master lookup."""

from __future__ import annotations

from datetime import datetime, timezone

from webcal_mcp.parser import expand_events, find_master, parse_calendar


def test_normalizes_events(simple_ics: bytes) -> None:
    cal = parse_calendar(simple_ics)
    events = expand_events(
        cal,
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 10, tzinfo=timezone.utc),
    )
    assert [e.uid for e in events] == ["evt-1@test", "evt-2@test", "evt-3@test"]

    standup = events[0]
    assert standup.summary == "Standup"
    assert standup.location == "Zoom"
    assert standup.all_day is False
    assert standup.categories == ("work", "meetings")
    assert standup.start.tzinfo is not None


def test_all_day_event_flagged(simple_ics: bytes) -> None:
    cal = parse_calendar(simple_ics)
    events = expand_events(
        cal,
        datetime(2026, 6, 4, tzinfo=timezone.utc),
        datetime(2026, 6, 7, tzinfo=timezone.utc),
    )
    [conf] = events
    assert conf.uid == "evt-3@test"
    assert conf.all_day is True


def test_window_filters_events(simple_ics: bytes) -> None:
    cal = parse_calendar(simple_ics)
    events = expand_events(
        cal,
        datetime(2026, 6, 2, tzinfo=timezone.utc),
        datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    assert [e.uid for e in events] == ["evt-2@test"]


def test_recurrence_expansion(recurring_ics: bytes) -> None:
    cal = parse_calendar(recurring_ics)
    events = expand_events(
        cal,
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    assert len(events) == 4
    assert all(e.summary == "Weekly sync" for e in events)
    # Each occurrence is exactly one week apart.
    gaps = {(b.start - a.start).days for a, b in zip(events, events[1:])}
    assert gaps == {7}


def test_find_master(simple_ics: bytes) -> None:
    cal = parse_calendar(simple_ics)
    master = find_master(cal, "evt-2@test")
    assert master is not None
    assert master.summary == "Lunch with Alice"
    assert find_master(cal, "nope") is None


def test_as_markdown_includes_key_fields(simple_ics: bytes) -> None:
    cal = parse_calendar(simple_ics)
    [standup, *_] = expand_events(
        cal,
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 2, tzinfo=timezone.utc),
    )
    md = standup.as_markdown()
    assert "## Standup" in md
    assert "Zoom" in md
    assert "evt-1@test" in md
    assert "Daily sync" in md
