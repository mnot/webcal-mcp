"""MCP server entry point and tool definitions."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from dateutil import parser as dateparse
from mcp.server.fastmcp import Context, FastMCP

from .config import CalendarConfig, Config, load_config, resolve_config_path
from .fetcher import IcsHttpSource
from .parser import Event
from .source import CalendarSource

Detail = Literal["brief", "full", "markdown"]

# We only use the context to emit log notifications, so the session/lifespan/
# request type parameters are immaterial here.
ToolContext = Context[Any, Any, Any]

DEFAULT_WINDOW_DAYS = 30
MAX_RESULTS = 500
# Recurrence expansion cost scales with the window width (a daily event over
# N days is N occurrences), so cap how wide a span a caller can ask for. ~5
# years is generous for real queries while keeping expansion bounded.
MAX_WINDOW_DAYS = 1830


class _ResourceGate:
    """A readers-writer gate over the registry's swappable resources.

    A tool call holds a shared *read* lease while it resolves sources and
    fetches through them; a config reload takes the exclusive *write* lease
    before swapping or closing those resources. This stops a reload from
    closing the shared httpx client out from under an in-flight request.
    Writers take priority, so a steady stream of reads can't starve a
    pending reload.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writers_waiting = 0

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._cond:
            # Defer to a pending/active writer rather than barging in.
            while self._writers_waiting:
                await self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        # Hold the condition lock across the whole critical section: once the
        # readers drain we never await again until the body completes, so no
        # new reader can slip in while resources are being swapped/closed.
        async with self._cond:
            self._writers_waiting += 1
            try:
                while self._readers:
                    await self._cond.wait()
                yield
            finally:
                # Always re-arm deferred readers, even if waiting was cancelled
                # or the body raised (e.g. an aclose() during reload). Skipping
                # the notify would strand a reader parked in read() forever.
                self._writers_waiting -= 1
                self._cond.notify_all()


class CalendarRegistry:
    def __init__(self, config: Config, config_path: Path | None = None) -> None:
        self._config = config
        self._config_path = config_path
        self._client = self._make_client(config)
        self._sources: dict[str, CalendarSource] = {
            name: self._build_source(c) for name, c in config.calendars.items()
        }
        self._reload_lock = asyncio.Lock()
        self._gate = _ResourceGate()
        self._config_mtime = self._read_mtime()

    def reading(self) -> AbstractAsyncContextManager[None]:
        """Shared lease held by a tool call while it fetches through sources.

        A reload waits for outstanding leases to drain before swapping or
        closing resources, so the shared client can't be closed mid-request.
        """
        return self._gate.read()

    def _read_mtime(self) -> float | None:
        if self._config_path is None:
            return None
        try:
            return self._config_path.stat().st_mtime
        except OSError:
            return None

    async def maybe_reload(self) -> None:
        """Reload the config if the file changed on disk since we last looked.

        Just a `stat`, so it's cheap enough to call before every tool
        invocation; the actual re-read only happens when the mtime moved.
        Lets config edits land without the client restarting the server.
        """
        mtime = self._read_mtime()
        if mtime is None or mtime == self._config_mtime:
            return
        # Record the mtime up front so a config that fails to parse isn't
        # re-read on every call; the next edit will bump the mtime again.
        self._config_mtime = mtime
        await self.reload()

    @staticmethod
    def _make_client(config: Config) -> httpx.AsyncClient:
        # Bound the connect phase separately so an unreachable host fails fast
        # instead of tying up the full (read-oriented) timeout just to connect.
        timeout = httpx.Timeout(
            config.http_timeout_seconds,
            connect=min(10.0, config.http_timeout_seconds),
        )
        return httpx.AsyncClient(
            timeout=timeout,
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

            # Swap resources under the write lease so no in-flight tool call
            # is still using the client/sources we're about to close.
            async with self._gate.write():
                # Global HTTP settings are baked into the client; if they
                # changed the client (and every ICS source) must be rebuilt.
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

    def resolve_many(self, name: str | list[str] | None) -> list[CalendarSource]:
        """Resolve to one or more sources, preserving order.

        - ``None`` (or an empty list) → every configured calendar
        - a single name → just that calendar
        - a list of names → those calendars, in the given order

        Raises ``ValueError`` naming any calendars that aren't configured.
        """
        if name is None:
            return list(self._sources.values())
        names = [name] if isinstance(name, str) else list(name)
        if not names:
            return list(self._sources.values())
        out: list[CalendarSource] = []
        unknown: list[str] = []
        for cal_name in names:
            src = self._sources.get(cal_name)
            if src is None:
                unknown.append(cal_name)
            else:
                out.append(src)
        if unknown:
            configured = ", ".join(sorted(self._sources)) or "(none)"
            bad = ", ".join(repr(u) for u in unknown)
            raise ValueError(f"Unknown calendar(s) {bad}. Configured: {configured}")
        return out

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
    if end_dt < start_dt:
        raise ValueError(f"end ({end!r}) is before start ({start!r})")
    if (end_dt - start_dt) > timedelta(days=MAX_WINDOW_DAYS):
        raise ValueError(
            f"Requested window exceeds the maximum of {MAX_WINDOW_DAYS} days; "
            "narrow the start/end range."
        )
    return start_dt, end_dt


def _matches(
    ev: Event,
    *,
    query: str | None,
    categories: list[str] | None,
    location: str | None,
) -> bool:
    if query:
        needle = query.lower()
        if needle not in ev.summary.lower() and needle not in ev.description.lower():
            return False
    if categories:
        wanted = {c.lower() for c in categories}
        if not wanted & {c.lower() for c in ev.categories}:
            return False
    if location and location.lower() not in ev.location.lower():
        return False
    return True


async def _gather_events(
    sources: list[CalendarSource],
    start: datetime,
    end: datetime,
    *,
    refresh: bool,
) -> tuple[list[tuple[str, Event]], list[tuple[str, str]]]:
    """Fetch from each source concurrently, best-effort, tagging by calendar.

    Returns the tagged events together with a list of ``(calendar, message)``
    for any sources that failed. A failed source is logged to stderr and
    skipped, so one unreachable calendar doesn't sink the whole query; the
    failures list lets the caller surface which ones were dropped. Raises only
    when every source fails — the caller then sees a real error rather than a
    silent empty list.
    """
    results = await asyncio.gather(
        *(src.events(start, end, refresh=refresh) for src in sources),
        return_exceptions=True,
    )
    tagged: list[tuple[str, Event]] = []
    failures: list[tuple[str, str]] = []
    for src, res in zip(sources, results):
        if isinstance(res, Exception):
            failures.append((src.name, str(res) or res.__class__.__name__))
        elif isinstance(res, BaseException):
            raise res  # don't swallow cancellation / system-exiting signals
        else:
            tagged.extend((src.name, ev) for ev in res)
    for name, msg in failures:
        print(f"webcal-mcp: failed to fetch calendar {name!r}: {msg}", file=sys.stderr)
    if failures and len(failures) == len(sources):
        detail = "; ".join(f"{name}: {msg}" for name, msg in failures)
        raise ValueError(f"All calendars failed to fetch: {detail}")
    return tagged, failures


async def _warn_failures(ctx: ToolContext, failures: list[tuple[str, str]]) -> None:
    """Tell the MCP client which calendars were skipped, if any.

    Partial failures are otherwise invisible to the caller (they only reach
    stderr), so a query that silently drops a calendar looks like it returned
    a complete result.
    """
    if not failures:
        return
    detail = "; ".join(f"{name} ({msg})" for name, msg in failures)
    await ctx.warning(f"Some calendars were skipped after failing to fetch: {detail}")


def _render(tagged: list[tuple[str, Event]], detail: Detail, *, show_calendar: bool) -> Any:
    def cal(name: str) -> str | None:
        return name if show_calendar else None

    if detail == "brief":
        return [e.as_brief(cal(name)) for name, e in tagged]
    if detail == "full":
        return [e.as_full(cal(name)) for name, e in tagged]
    if detail == "markdown":
        return "\n\n---\n\n".join(e.as_markdown(cal(name)) for name, e in tagged)
    raise ValueError(f"Unknown detail mode {detail!r}")


def build_server(registry: CalendarRegistry) -> FastMCP:
    mcp = FastMCP("webcal-mcp")

    @mcp.tool()
    async def list_calendars() -> list[dict[str, Any]]:
        """List the configured calendars.

        Re-reads the config file first, so calendars added or changed
        since the server started are picked up without a restart.
        """
        await registry.maybe_reload()
        async with registry.reading():
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
        ctx: ToolContext,
        calendar: str | list[str] | None = None,
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

        - `calendar`: a name from `list_calendars`, a list of names, or omitted.
          Omitted (None) means *all* configured calendars; a list means just
          those. When more than one calendar is queried, each event carries a
          `calendar` field (a `**Calendar:**` line in markdown) and events are
          merged and sorted by start time.
        - `start` / `end`: ISO date or datetime. Either may be omitted for an
          open-ended window; a default 30-day window applies if both are omitted.
          The span between an explicit start and end may not exceed ~5 years.
        - `query`: case-insensitive substring match against summary + description.
        - `categories`: match if the event has any of these categories.
        - `location`: case-insensitive substring match against location.
        - `detail`: 'brief' (uid/title/dates), 'full' (all fields as JSON), or
          'markdown' (LLM-friendly formatted block).
        - `limit`: cap on returned events (default 100, max 500), applied after
          merging and sorting across calendars.
        - `refresh`: if True, bypass the TTL cache and re-fetch from the
          upstream calendar. Use when the user just edited the calendar and
          the cached copy may be stale.
        """
        await registry.maybe_reload()
        start_dt, end_dt = _resolve_window(start, end)
        async with registry.reading():
            sources = registry.resolve_many(calendar)
            n_sources = len(sources)
            tagged, failures = await _gather_events(sources, start_dt, end_dt, refresh=refresh)
        await _warn_failures(ctx, failures)
        tagged = [
            t
            for t in tagged
            if _matches(t[1], query=query, categories=categories, location=location)
        ]
        tagged.sort(key=lambda t: t[1].start)
        limit = max(1, min(limit, MAX_RESULTS))
        tagged = tagged[:limit]
        return _render(tagged, detail, show_calendar=n_sources > 1)

    @mcp.tool()
    async def events_on(
        ctx: ToolContext,
        date: str,
        calendar: str | list[str] | None = None,
        detail: Detail = "brief",
        refresh: bool = False,
    ) -> Any:
        """Return events occurring on a specific date (YYYY-MM-DD).

        `calendar` works as in `list_events`: a name, a list of names, or
        omitted for *all* configured calendars. When more than one calendar
        is queried, events carry a `calendar` field and are sorted by start.

        Pass `refresh=True` to bypass the TTL cache and re-fetch the
        upstream calendar.
        """
        await registry.maybe_reload()
        day = _parse_when(date)
        if day is None:
            raise ValueError("date is required")
        start = datetime.combine(day.date(), time.min, tzinfo=day.tzinfo)
        end = start + timedelta(days=1)
        async with registry.reading():
            sources = registry.resolve_many(calendar)
            n_sources = len(sources)
            tagged, failures = await _gather_events(sources, start, end, refresh=refresh)
        await _warn_failures(ctx, failures)
        tagged.sort(key=lambda t: t[1].start)
        return _render(tagged, detail, show_calendar=n_sources > 1)

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
        await registry.maybe_reload()
        async with registry.reading():
            source = registry.resolve(calendar)
            name = source.name
            event = await source.get_event(uid, refresh=refresh)
        if event is None:
            return None
        return _render([(name, event)], detail, show_calendar=False)

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
