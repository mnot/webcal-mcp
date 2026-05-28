"""HTTP-backed iCalendar source with TTL + ETag/Last-Modified caching."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
import icalendar

from .config import CalendarConfig
from .parser import Event, expand_events, find_master, parse_calendar
from .source import CalendarSource


@dataclass
class _CacheEntry:
    calendar: icalendar.Calendar
    fetched_at: float
    etag: str | None
    last_modified: str | None


class IcsHttpSource(CalendarSource):
    """Read-only iCalendar source backed by an HTTP(S) URL."""

    def __init__(
        self,
        config: CalendarConfig,
        client: httpx.AsyncClient,
        ttl_seconds: int | None = None,
    ) -> None:
        self.name = config.name
        self._config = config
        self._client = client
        self._ttl = ttl_seconds if ttl_seconds is not None else config.ttl_seconds
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    async def _calendar(self) -> icalendar.Calendar:
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and (now - self._cache.fetched_at) < self._ttl:
                return self._cache.calendar

            headers: dict[str, str] = {}
            if self._cache is not None:
                if self._cache.etag:
                    headers["If-None-Match"] = self._cache.etag
                if self._cache.last_modified:
                    headers["If-Modified-Since"] = self._cache.last_modified

            resp = await self._client.get(self._config.http_url, headers=headers)
            if resp.status_code == 304 and self._cache is not None:
                # Refresh the cache window without re-parsing.
                self._cache = _CacheEntry(
                    calendar=self._cache.calendar,
                    fetched_at=now,
                    etag=self._cache.etag,
                    last_modified=self._cache.last_modified,
                )
                return self._cache.calendar
            resp.raise_for_status()
            cal = parse_calendar(resp.content)
            self._cache = _CacheEntry(
                calendar=cal,
                fetched_at=now,
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
            )
            return cal

    async def events(self, start: datetime, end: datetime) -> list[Event]:
        cal = await self._calendar()
        return expand_events(cal, start, end)

    async def get_event(self, uid: str) -> Event | None:
        cal = await self._calendar()
        return find_master(cal, uid)
