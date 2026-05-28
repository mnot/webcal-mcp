"""Tests for server helpers and tool registration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from webcal_mcp.config import CalendarConfig, Config
from webcal_mcp.parser import Event
from webcal_mcp.server import (
    DEFAULT_WINDOW_DAYS,
    CalendarRegistry,
    _filter,
    _resolve_window,
    build_server,
)


def _ev(uid: str, **kw: object) -> Event:
    base = {
        "uid": uid,
        "summary": "",
        "start": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "end": datetime(2026, 6, 1, 1, tzinfo=timezone.utc),
        "all_day": False,
    }
    base.update(kw)
    return Event(**base)  # type: ignore[arg-type]


def test_resolve_window_defaults_when_empty() -> None:
    start, end = _resolve_window(None, None)
    assert (end - start) == timedelta(days=DEFAULT_WINDOW_DAYS)


def test_resolve_window_open_start() -> None:
    start, end = _resolve_window(None, "2026-06-30")
    assert (end - start).days >= DEFAULT_WINDOW_DAYS - 1


def test_resolve_window_open_end() -> None:
    start, end = _resolve_window("2026-06-01", None)
    assert (end - start) == timedelta(days=DEFAULT_WINDOW_DAYS)


def test_resolve_window_pushes_end_to_end_of_day() -> None:
    _, end = _resolve_window("2026-06-01", "2026-06-02")
    assert end.hour == 23 and end.minute == 59


def test_filter_query_matches_summary_and_description() -> None:
    events = [
        _ev("a", summary="Standup"),
        _ev("b", summary="Lunch", description="with Alice"),
        _ev("c", summary="Other"),
    ]
    assert {e.uid for e in _filter(events, query="alice", categories=None, location=None)} == {"b"}
    assert {e.uid for e in _filter(events, query="stand", categories=None, location=None)} == {"a"}


def test_filter_categories_intersection() -> None:
    events = [
        _ev("a", categories=("work",)),
        _ev("b", categories=("personal",)),
        _ev("c", categories=("work", "urgent")),
    ]
    got = _filter(events, query=None, categories=["WORK"], location=None)
    assert {e.uid for e in got} == {"a", "c"}


def test_filter_location_substring() -> None:
    events = [
        _ev("a", location="Cafe Roma"),
        _ev("b", location="Office"),
    ]
    got = _filter(events, query=None, categories=None, location="cafe")
    assert {e.uid for e in got} == {"a"}


def _registry(*names: str) -> CalendarRegistry:
    cals = {n: CalendarConfig(name=n, url=f"https://x/{n}.ics") for n in names}
    return CalendarRegistry(Config(calendars=cals))


def test_resolve_requires_name_when_multiple() -> None:
    reg = _registry("a", "b")
    try:
        with pytest.raises(ValueError, match="Multiple calendars"):
            reg.resolve(None)
        with pytest.raises(ValueError, match="Unknown calendar"):
            reg.resolve("c")
        assert reg.resolve("a").name == "a"
    finally:
        import asyncio

        asyncio.run(reg.aclose())


def test_resolve_defaults_to_only_calendar() -> None:
    reg = _registry("solo")
    try:
        assert reg.resolve(None).name == "solo"
    finally:
        import asyncio

        asyncio.run(reg.aclose())


@pytest.mark.asyncio
async def test_build_server_registers_expected_tools() -> None:
    reg = _registry("solo")
    try:
        server = build_server(reg)
        tools = await server.list_tools()
        assert {t.name for t in tools} == {
            "list_calendars",
            "list_events",
            "events_on",
            "get_event",
        }
    finally:
        await reg.aclose()
