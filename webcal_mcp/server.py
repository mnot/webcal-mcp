"""MCP server entry point and tool definitions."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from dateutil import parser as dateparse
from mcp.server.fastmcp import FastMCP

from .config import CalendarConfig, Config, load_config, resolve_config_path
from .fetcher import IcsHttpSource
from .parser import Event
from .source import CalendarSource

Detail = Literal["brief", "full", "markdown"]

DEFAULT_WINDOW_DAYS = 30
MAX_RESULTS = 500


class CalendarRegistry:
    def __init__(self, config: Config, config_path: Path | None = None) -> None:
        self._config = config
        self._config_path = config_path
        self._client = self._make_client(config)
        self._sources: dict[str, CalendarSource] = {
            name: self._build_source(c) for name, c in config.calendars.items()
        }
        self._reload_lock = asyncio.Lock()

    @staticmethod
    def _make_client(config: Config) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=config.http_timeout_seconds,
            headers={"User-Agent": config.user_agent},
            follow_redirects=True,
        )

    async def reload(self) -> None:
        """Re-read the config file and apply any changes in place.

        Sources whose config is unchanged are kept as-is (preserving their
        caches); new and changed calendars are rebuilt, removed ones are
        closed. If the file is missing or invalid, the current config is
        left untouched. Called from `list_calendars` so config edits land
        without restarting the server.
        """
        async with self._reload_lock:
            try:
                new_config = load_config(self._config_path)
            except (FileNotFoundError, ValueError, OSError) as exc:
                print(
                    f"webcal-mcp: config reload failed, keeping current config: {exc}",
                    file=sys.stderr,
                )
                return
            if new_config == self._config:
                return

            # Global HTTP settings are baked into the client; if they changed
            # the client (and therefore every ICS source) must be rebuilt.
            client_changed = (
                new_config.http_timeout_seconds != self._config.http_timeout_seconds
                or new_config.user_agent != self._config.user_agent
            )
            old_client = self._client
            if client_changed:
                self._client = self._make_client(new_config)

            new_sources: dict[str, CalendarSource] = {}
            for name, cfg in new_config.calendars.items():
                reuse = (
                    not client_changed
                    and name in self._sources
                    and self._config.calendars.get(name) == cfg
                )
                new_sources[name] = self._sources[name] if reuse else self._build_source(cfg)

            old_sources = self._sources
            self._sources = new_sources
            self._config = new_config

            for name, src in old_sources.items():
                if src is not new_sources.get(name):
                    await src.aclose()
            if client_changed:
                await old_client.aclose()

    def _build_source(self, cfg: CalendarConfig) -> CalendarSource:
        if cfg.source == "ics":
            return IcsHttpSource(cfg, self._client)
        if cfg.source == "eventkit":
            # Imported lazily so non-Darwin platforms (and the import-time
            # path on Darwin without PyObjC installed) don't pay the cost.
            from .eventkit import EventKitSource  # pylint: disable=import-outside-toplevel

            return EventKitSource(cfg)
        raise ValueError(f"Unknown calendar source {cfg.source!r} for {cfg.name!r}")

    @property
    def config(self) -> Config:
        return self._config

    def resolve(self, name: str | None) -> CalendarSource:
        if name is None:
            default = self._config.default_calendar
            if default is None:
                names = ", ".join(sorted(self._sources)) or "(none)"
                raise ValueError(f"Multiple calendars configured; specify one of: {names}")
            name = default
        if name not in self._sources:
            names = ", ".join(sorted(self._sources)) or "(none)"
            raise ValueError(f"Unknown calendar {name!r}. Configured: {names}")
        return self._sources[name]

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_when(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if value is None:
        return None
    try:
        dt = dateparse.parse(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Could not parse date/time {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # If the caller passed a date-only string, dateutil returns midnight.
    # For an end bound, push to end of day so the inclusive intent is met.
    if end_of_day and dt.time() == time(0, 0):
        dt = datetime.combine(dt.date(), time(23, 59, 59, 999999), tzinfo=dt.tzinfo)
    return dt


def _resolve_window(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    start_dt = _parse_when(start)
    end_dt = _parse_when(end, end_of_day=True)
    if start_dt is None and end_dt is None:
        start_dt = datetime.now(timezone.utc)
        end_dt = start_dt + timedelta(days=DEFAULT_WINDOW_DAYS)
    elif start_dt is None:
        assert end_dt is not None
        start_dt = end_dt - timedelta(days=DEFAULT_WINDOW_DAYS)
    elif end_dt is None:
        end_dt = start_dt + timedelta(days=DEFAULT_WINDOW_DAYS)
    return start_dt, end_dt


def _filter(
    events: list[Event],
    *,
    query: str | None,
    categories: list[str] | None,
    location: str | None,
) -> list[Event]:
    out = events
    if query:
        needle = query.lower()
        out = [ev for ev in out if needle in ev.summary.lower() or needle in ev.description.lower()]
    if categories:
        wanted = {c.lower() for c in categories}
        out = [ev for ev in out if wanted & {c.lower() for c in ev.categories}]
    if location:
        loc = location.lower()
        out = [ev for ev in out if loc in ev.location.lower()]
    return out


def _render(events: list[Event], detail: Detail) -> Any:
    if detail == "brief":
        return [e.as_brief() for e in events]
    if detail == "full":
        return [e.as_full() for e in events]
    if detail == "markdown":
        return "\n\n---\n\n".join(e.as_markdown() for e in events)
    raise ValueError(f"Unknown detail mode {detail!r}")


def build_server(registry: CalendarRegistry) -> FastMCP:
    mcp = FastMCP("webcal-mcp")

    @mcp.tool()
    async def list_calendars() -> list[dict[str, Any]]:
        """List the configured calendars.

        Re-reads the config file first, so calendars added or changed
        since the server started are picked up without a restart.
        """
        await registry.reload()
        return [
            {
                "name": c.name,
                "description": c.description,
                "writable": registry.resolve(c.name).writable,
            }
            for c in registry.config.calendars.values()
        ]

    @mcp.tool()
    async def list_events(
        calendar: str | None = None,
        start: str | None = None,
        end: str | None = None,
        query: str | None = None,
        categories: list[str] | None = None,
        location: str | None = None,
        detail: Detail = "brief",
        limit: int = 100,
        refresh: bool = False,
    ) -> Any:
        """List events in a date range, with optional filters.

        - `calendar`: name from `list_calendars`. Optional if only one is configured.
        - `start` / `end`: ISO date or datetime. Either may be omitted for an
          open-ended window; a default 30-day window applies if both are omitted.
        - `query`: case-insensitive substring match against summary + description.
        - `categories`: match if the event has any of these categories.
        - `location`: case-insensitive substring match against location.
        - `detail`: 'brief' (uid/title/dates), 'full' (all fields as JSON), or
          'markdown' (LLM-friendly formatted block).
        - `limit`: cap on returned events (default 100, max 500).
        - `refresh`: if True, bypass the TTL cache and re-fetch from the
          upstream calendar. Use when the user just edited the calendar and
          the cached copy may be stale.
        """
        source = registry.resolve(calendar)
        start_dt, end_dt = _resolve_window(start, end)
        events = await source.events(start_dt, end_dt, refresh=refresh)
        events = _filter(events, query=query, categories=categories, location=location)
        limit = max(1, min(limit, MAX_RESULTS))
        events = events[:limit]
        return _render(events, detail)

    @mcp.tool()
    async def events_on(
        date: str,
        calendar: str | None = None,
        detail: Detail = "brief",
        refresh: bool = False,
    ) -> Any:
        """Return events occurring on a specific date (YYYY-MM-DD).

        Pass `refresh=True` to bypass the TTL cache and re-fetch the
        upstream calendar.
        """
        source = registry.resolve(calendar)
        day = _parse_when(date)
        if day is None:
            raise ValueError("date is required")
        start = datetime.combine(day.date(), time.min, tzinfo=day.tzinfo)
        end = start + timedelta(days=1)
        events = await source.events(start, end, refresh=refresh)
        return _render(events, detail)

    @mcp.tool()
    async def get_event(
        uid: str,
        calendar: str | None = None,
        detail: Detail = "full",
        refresh: bool = False,
    ) -> Any:
        """Look up a single event by UID. Returns None if not found.

        Pass `refresh=True` to bypass the TTL cache and re-fetch the
        upstream calendar.
        """
        source = registry.resolve(calendar)
        event = await source.get_event(uid, refresh=refresh)
        if event is None:
            return None
        return _render([event], detail) if detail != "markdown" else event.as_markdown()

    return mcp


def _list_eventkit() -> int:
    # Imported lazily so the server entry point doesn't require PyObjC
    # just to print a help message.
    from .eventkit import (  # pylint: disable=import-outside-toplevel
        EventKitNotAvailable,
        list_eventkit_calendars,
    )

    try:
        cals = list_eventkit_calendars()
    except EventKitNotAvailable as exc:
        print(f"webcal-mcp: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    if not cals:
        print("(no EventKit calendars found)")
        return 0
    width = max(len(cal["title"]) for cal in cals)
    for cal in cals:
        print(f"{cal['title']:<{width}}  {cal['identifier']}  [{cal['source']}]")
    return 0


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "list-eventkit":
        sys.exit(_list_eventkit())

    try:
        config_path = resolve_config_path()
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"webcal-mcp: {exc}", file=sys.stderr)
        sys.exit(2)

    registry = CalendarRegistry(config, config_path)
    server = build_server(registry)

    try:
        server.run()
    except KeyboardInterrupt:
        # Plain Ctrl-C at the terminal — exit quietly without a traceback.
        pass
    finally:
        try:
            asyncio.run(registry.aclose())
        except (RuntimeError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    main()
