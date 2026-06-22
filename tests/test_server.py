"""Tests for server helpers and tool registration."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from webcal_mcp.config import CalendarConfig, Config, load_config
from webcal_mcp.parser import Event
from webcal_mcp.server import (
    DEFAULT_WINDOW_DAYS,
    CalendarRegistry,
    _filter,
    _gather_events,
    _render,
    _resolve_window,
    build_server,
)
from webcal_mcp.source import CalendarSource


class _FakeSource(CalendarSource):
    """In-memory source for exercising the multi-calendar paths."""

    def __init__(self, name: str, events: list[Event] | None = None, error: str | None = None):
        self.name = name
        self._events = events or []
        self._error = error

    async def events(self, start, end, *, refresh=False):  # type: ignore[no-untyped-def]
        if self._error is not None:
            raise RuntimeError(self._error)
        return list(self._events)

    async def get_event(self, uid, *, refresh=False):  # type: ignore[no-untyped-def]
        return next((e for e in self._events if e.uid == uid), None)


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


def test_resolve_many_none_returns_all() -> None:
    reg = _registry("a", "b")
    try:
        assert [s.name for s in reg.resolve_many(None)] == ["a", "b"]
        assert [s.name for s in reg.resolve_many([])] == ["a", "b"]
    finally:
        import asyncio

        asyncio.run(reg.aclose())


def test_resolve_many_single_and_list_preserve_order() -> None:
    reg = _registry("a", "b", "c")
    try:
        assert [s.name for s in reg.resolve_many("b")] == ["b"]
        assert [s.name for s in reg.resolve_many(["c", "a"])] == ["c", "a"]
    finally:
        import asyncio

        asyncio.run(reg.aclose())


def test_resolve_many_reports_unknown() -> None:
    reg = _registry("a", "b")
    try:
        with pytest.raises(ValueError, match="Unknown calendar.*'x'"):
            reg.resolve_many(["a", "x"])
    finally:
        import asyncio

        asyncio.run(reg.aclose())


def _at(uid: str, day: int) -> Event:
    return _ev(uid, start=datetime(2026, 6, day, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_gather_events_tags_and_collects() -> None:
    src_a = _FakeSource("a", [_at("a1", 2)])
    src_b = _FakeSource("b", [_at("b1", 1)])
    win = _resolve_window(None, None)
    tagged = await _gather_events([src_a, src_b], *win, refresh=False)
    assert {(name, e.uid) for name, e in tagged} == {("a", "a1"), ("b", "b1")}


@pytest.mark.asyncio
async def test_gather_events_partial_failure_is_best_effort() -> None:
    good = _FakeSource("good", [_at("g1", 1)])
    bad = _FakeSource("bad", error="boom")
    win = _resolve_window(None, None)
    tagged = await _gather_events([good, bad], *win, refresh=False)
    assert [(name, e.uid) for name, e in tagged] == [("good", "g1")]


@pytest.mark.asyncio
async def test_gather_events_raises_when_all_fail() -> None:
    bad1 = _FakeSource("x", error="boom")
    bad2 = _FakeSource("y", error="bust")
    win = _resolve_window(None, None)
    with pytest.raises(ValueError, match="All calendars failed"):
        await _gather_events([bad1, bad2], *win, refresh=False)


def test_render_tags_calendar_when_requested() -> None:
    tagged = [("work", _ev("a")), ("home", _ev("b"))]
    brief = _render(tagged, "brief", show_calendar=True)
    assert [row["calendar"] for row in brief] == ["work", "home"]
    untagged = _render(tagged, "brief", show_calendar=False)
    assert all("calendar" not in row for row in untagged)
    md = _render(tagged, "markdown", show_calendar=True)
    assert "**Calendar:** work" in md and "**Calendar:** home" in md


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


def _write_config(path: Path, body: str) -> Path:
    path.write_text(dedent(body))
    return path


@pytest.mark.asyncio
async def test_reload_picks_up_new_calendar(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "config.toml",
        """\
        [calendars.a]
        url = "https://example.com/a.ics"
        """,
    )
    reg = CalendarRegistry(load_config(cfg_path), cfg_path)
    try:
        assert set(reg.config.calendars) == {"a"}
        _write_config(
            cfg_path,
            """\
            [calendars.a]
            url = "https://example.com/a.ics"

            [calendars.b]
            url = "https://example.com/b.ics"
            """,
        )
        await reg.reload()
        assert set(reg.config.calendars) == {"a", "b"}
    finally:
        await reg.aclose()


@pytest.mark.asyncio
async def test_reload_preserves_unchanged_source(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "config.toml",
        """\
        [calendars.a]
        url = "https://example.com/a.ics"

        [calendars.b]
        url = "https://example.com/b.ics"
        """,
    )
    reg = CalendarRegistry(load_config(cfg_path), cfg_path)
    try:
        src_a = reg.resolve("a")
        src_b = reg.resolve("b")
        # Change only calendar b's URL.
        _write_config(
            cfg_path,
            """\
            [calendars.a]
            url = "https://example.com/a.ics"

            [calendars.b]
            url = "https://example.com/b-new.ics"
            """,
        )
        await reg.reload()
        assert reg.resolve("a") is src_a  # unchanged → same object, cache kept
        assert reg.resolve("b") is not src_b  # changed → rebuilt
    finally:
        await reg.aclose()


@pytest.mark.asyncio
async def test_reload_drops_removed_calendar(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "config.toml",
        """\
        [calendars.a]
        url = "https://example.com/a.ics"

        [calendars.b]
        url = "https://example.com/b.ics"
        """,
    )
    reg = CalendarRegistry(load_config(cfg_path), cfg_path)
    try:
        _write_config(
            cfg_path,
            """\
            [calendars.a]
            url = "https://example.com/a.ics"
            """,
        )
        await reg.reload()
        assert set(reg.config.calendars) == {"a"}
        with pytest.raises(ValueError, match="Unknown calendar"):
            reg.resolve("b")
    finally:
        await reg.aclose()


@pytest.mark.asyncio
async def test_reload_keeps_config_when_file_invalid(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "config.toml",
        """\
        [calendars.a]
        url = "https://example.com/a.ics"
        """,
    )
    reg = CalendarRegistry(load_config(cfg_path), cfg_path)
    try:
        cfg_path.write_text("this is not = valid toml [[[")
        await reg.reload()  # must not raise
        assert set(reg.config.calendars) == {"a"}  # original kept
    finally:
        await reg.aclose()


@pytest.mark.asyncio
async def test_maybe_reload_picks_up_change_on_new_mtime(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "config.toml",
        """\
        [calendars.a]
        url = "https://example.com/a.ics"
        """,
    )
    reg = CalendarRegistry(load_config(cfg_path), cfg_path)
    try:
        st = cfg_path.stat()
        _write_config(
            cfg_path,
            """\
            [calendars.a]
            url = "https://example.com/a.ics"

            [calendars.b]
            url = "https://example.com/b.ics"
            """,
        )
        # Force a distinct mtime so the gate fires regardless of clock granularity.
        os.utime(cfg_path, (st.st_atime + 1, st.st_mtime + 1))
        await reg.maybe_reload()
        assert set(reg.config.calendars) == {"a", "b"}
    finally:
        await reg.aclose()


@pytest.mark.asyncio
async def test_maybe_reload_skips_when_mtime_unchanged(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "config.toml",
        """\
        [calendars.a]
        url = "https://example.com/a.ics"
        """,
    )
    reg = CalendarRegistry(load_config(cfg_path), cfg_path)
    try:
        st = cfg_path.stat()
        # Change content but restore the original mtime: the gate must not fire.
        _write_config(
            cfg_path,
            """\
            [calendars.a]
            url = "https://example.com/a.ics"

            [calendars.b]
            url = "https://example.com/b.ics"
            """,
        )
        os.utime(cfg_path, (st.st_atime, st.st_mtime))
        await reg.maybe_reload()
        assert set(reg.config.calendars) == {"a"}
    finally:
        await reg.aclose()


@pytest.mark.asyncio
async def test_reload_rebuilds_client_when_global_settings_change(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "config.toml",
        """\
        http_timeout_seconds = 30.0

        [calendars.a]
        url = "https://example.com/a.ics"
        """,
    )
    reg = CalendarRegistry(load_config(cfg_path), cfg_path)
    try:
        src_a = reg.resolve("a")
        _write_config(
            cfg_path,
            """\
            http_timeout_seconds = 5.0

            [calendars.a]
            url = "https://example.com/a.ics"
            """,
        )
        await reg.reload()
        # Even though calendar a is unchanged, a new client forces a rebuild.
        assert reg.resolve("a") is not src_a
        assert reg.config.http_timeout_seconds == 5.0
    finally:
        await reg.aclose()
