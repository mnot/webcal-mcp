"""EventKit-backed read-only CalendarSource (macOS only).

Reads any calendar that's set up in Calendar.app — iCloud, local,
subscribed `.ics`, CalDAV, Google — via the system EventKit framework.
First use triggers a TCC permission prompt for Calendar access; the
grant is bound to the binary that calls it (e.g. the pipx-installed
`webcal-mcp` script's Python interpreter).
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Any

from .config import CalendarConfig
from .parser import Event
from .source import CalendarSource

# EKEventStatus constants (mirrored here so the module imports on non-Darwin).
_STATUS_NAMES = {0: "", 1: "CONFIRMED", 2: "TENTATIVE", 3: "CANCELLED"}


class EventKitNotAvailable(RuntimeError):
    """EventKit can't be used (not installed, access denied, calendar missing)."""


def _import_eventkit() -> tuple[Any, Any, Any]:
    try:
        # pylint: disable=import-outside-toplevel
        from EventKit import (  # type: ignore
            EKEntityTypeEvent,
            EKEventStore,
        )
        from Foundation import NSDate  # type: ignore
    except ImportError as exc:
        raise EventKitNotAvailable(
            "EventKit (pyobjc-framework-EventKit) is not installed. "
            "This source only works on macOS."
        ) from exc
    return EKEventStore, EKEntityTypeEvent, NSDate


class EventKitSource(CalendarSource):
    """Read-only source backed by an EKCalendar in the local EventKit store."""

    def __init__(self, config: CalendarConfig) -> None:
        self.name = config.name
        self._config = config
        self._identifier = config.identifier or config.name
        self._store: Any = None
        self._calendar: Any = None
        self._entity_type: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_ready(self) -> None:
        if self._store is not None and self._calendar is not None:
            return
        async with self._lock:
            if self._store is not None and self._calendar is not None:
                return
            await asyncio.to_thread(self._init_store_blocking)

    def _init_store_blocking(self) -> None:
        ek_store_cls, entity_type, _ = _import_eventkit()
        store = ek_store_cls.alloc().init()

        done = threading.Event()
        result: dict[str, Any] = {"granted": False, "error": None}

        def completion(granted: bool, error: Any) -> None:
            result["granted"] = bool(granted)
            result["error"] = error
            done.set()

        # macOS 14+ split read-only vs write access; fall back to the older API.
        if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(completion)
        else:
            store.requestAccessToEntityType_completion_(entity_type, completion)

        if not done.wait(timeout=30):
            raise EventKitNotAvailable(
                f"Timed out waiting for Calendar access prompt (source {self.name!r})."
            )
        if not result["granted"]:
            err = result["error"]
            suffix = f" ({err})" if err is not None else ""
            raise EventKitNotAvailable(
                f"Calendar access denied for source {self.name!r}. Grant access in "
                "System Settings → Privacy & Security → Calendars." + suffix
            )

        cal = self._find_calendar(store, entity_type)
        self._store = store
        self._calendar = cal
        self._entity_type = entity_type

    def _reset_store(self) -> None:
        """Drop the store's in-process snapshot so the next read reflects
        external edits.

        A long-lived ``EKEventStore`` is a snapshot: changes made by another
        process (Calendar.app, the system sync daemon) are invisible until the
        store is told to reset. ``refreshSourcesIfNecessary`` only pulls new
        data from *remote* sources into the local database; ``reset`` is what
        drops the cached objects so a subsequent fetch re-reads from disk.
        ``reset`` also invalidates previously fetched calendars, so we
        re-resolve our handle afterwards.
        """
        self._store.refreshSourcesIfNecessary()
        self._store.reset()
        self._calendar = self._find_calendar(self._store, self._entity_type)

    async def _refresh(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._reset_store)

    def _find_calendar(self, store: Any, entity_type: Any) -> Any:
        cals = list(store.calendarsForEntityType_(entity_type) or [])
        ident = self._identifier
        for cal in cals:
            if str(cal.calendarIdentifier()) == ident:
                return cal
        matches = [c for c in cals if str(c.title()) == ident]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise EventKitNotAvailable(
                f"EventKit identifier {ident!r} matches {len(matches)} calendars; "
                "use the calendar's UUID instead."
            )
        ci_matches = [c for c in cals if str(c.title()).lower() == ident.lower()]
        if len(ci_matches) == 1:
            return ci_matches[0]
        available = ", ".join(sorted({str(c.title()) for c in cals})) or "(none)"
        raise EventKitNotAvailable(
            f"No EventKit calendar matches {ident!r}. Available: {available}"
        )

    async def events(self, start: datetime, end: datetime, *, refresh: bool = False) -> list[Event]:
        await self._ensure_ready()
        if refresh:
            await self._refresh()
        return await asyncio.to_thread(self._events_blocking, start, end)

    def _events_blocking(self, start: datetime, end: datetime) -> list[Event]:
        _, _, ns_date_cls = _import_eventkit()
        ns_start = ns_date_cls.dateWithTimeIntervalSince1970_(start.timestamp())
        ns_end = ns_date_cls.dateWithTimeIntervalSince1970_(end.timestamp())
        predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            ns_start, ns_end, [self._calendar]
        )
        ek_events = list(self._store.eventsMatchingPredicate_(predicate) or [])
        out = [_to_event(e) for e in ek_events]
        out.sort(key=lambda e: e.start)
        return out

    async def get_event(self, uid: str, *, refresh: bool = False) -> Event | None:
        await self._ensure_ready()
        if refresh:
            await self._refresh()
        return await asyncio.to_thread(self._get_event_blocking, uid)

    def _get_event_blocking(self, uid: str) -> Event | None:
        items = list(self._store.calendarItemsWithExternalIdentifier_(uid) or [])
        for item in items:
            if item.calendar() == self._calendar:
                return _to_event(item)
        if items:
            return _to_event(items[0])
        return None


def _to_event(ek: Any) -> Event:
    start = datetime.fromtimestamp(ek.startDate().timeIntervalSince1970(), tz=timezone.utc)
    end = datetime.fromtimestamp(ek.endDate().timeIntervalSince1970(), tz=timezone.utc)
    organizer = ""
    org = ek.organizer()
    if org is not None:
        organizer = str(org.name() or org.URL() or "")
    attendees: tuple[str, ...] = ()
    ek_attendees = ek.attendees()
    if ek_attendees:
        attendees = tuple(str(a.name() or a.URL() or "") for a in ek_attendees)
    url = ""
    ek_url = ek.URL()
    if ek_url is not None:
        url = str(ek_url)
    uid = str(ek.calendarItemExternalIdentifier() or ek.calendarItemIdentifier())
    return Event(
        uid=uid,
        summary=str(ek.title() or ""),
        start=start,
        end=end,
        all_day=bool(ek.isAllDay()),
        description=str(ek.notes() or ""),
        location=str(ek.location() or ""),
        organizer=organizer,
        attendees=attendees,
        status=_STATUS_NAMES.get(int(ek.status()), ""),
        url=url,
        categories=(),
    )


def list_eventkit_calendars() -> list[dict[str, str]]:
    """Return [{title, identifier, source}] for every EventKit calendar.

    Used by the `webcal-mcp list-eventkit` CLI helper to let users
    discover the value to put in `identifier`. Triggers the TCC prompt
    on first use.
    """
    ek_store_cls, entity_type, _ = _import_eventkit()
    store = ek_store_cls.alloc().init()

    done = threading.Event()
    result: dict[str, Any] = {"granted": False, "error": None}

    def completion(granted: bool, error: Any) -> None:
        result["granted"] = bool(granted)
        result["error"] = error
        done.set()

    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(completion)
    else:
        store.requestAccessToEntityType_completion_(entity_type, completion)
    done.wait(timeout=30)
    if not result["granted"]:
        raise EventKitNotAvailable("Calendar access denied.")

    cals = list(store.calendarsForEntityType_(entity_type) or [])
    return [
        {
            "title": str(c.title()),
            "identifier": str(c.calendarIdentifier()),
            "source": str(c.source().title()) if c.source() is not None else "",
        }
        for c in cals
    ]
