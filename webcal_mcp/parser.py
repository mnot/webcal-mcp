"""ICS parsing and event normalisation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import icalendar
import recurring_ical_events  # type: ignore[import-untyped]


@dataclass(frozen=True)
class Event:
    uid: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    description: str = ""
    location: str = ""
    organizer: str = ""
    attendees: tuple[str, ...] = ()
    status: str = ""
    url: str = ""
    categories: tuple[str, ...] = ()
    recurrence_id: str = ""

    def as_brief(self, calendar: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "uid": self.uid,
            "summary": self.summary,
            "start": _iso(self.start, self.all_day),
            "end": _iso(self.end, self.all_day),
            "all_day": self.all_day,
        }
        if calendar is not None:
            out["calendar"] = calendar
        return out

    def as_full(self, calendar: str | None = None) -> dict[str, Any]:
        out = self.as_brief(calendar)
        out.update(
            {
                "description": self.description,
                "location": self.location,
                "organizer": self.organizer,
                "attendees": list(self.attendees),
                "status": self.status,
                "url": self.url,
                "categories": list(self.categories),
            }
        )
        if self.recurrence_id:
            out["recurrence_id"] = self.recurrence_id
        return out

    def as_markdown(self, calendar: str | None = None) -> str:
        """Compact LLM-friendly rendering."""
        lines = [f"## {self.summary or '(no title)'}"]
        if calendar is not None:
            lines.append(f"**Calendar:** {calendar}")
        when = f"{_iso(self.start, self.all_day)} → {_iso(self.end, self.all_day)}"
        if self.all_day:
            when += "  (all day)"
        lines.append(f"**When:** {when}")
        if self.location:
            lines.append(f"**Where:** {self.location}")
        if self.organizer:
            lines.append(f"**Organizer:** {self.organizer}")
        if self.attendees:
            lines.append(f"**Attendees:** {', '.join(self.attendees)}")
        if self.status:
            lines.append(f"**Status:** {self.status}")
        if self.categories:
            lines.append(f"**Categories:** {', '.join(self.categories)}")
        if self.url:
            lines.append(f"**URL:** {self.url}")
        lines.append(f"**UID:** {self.uid}")
        if self.description:
            lines.append("")
            lines.append(self.description.strip())
        return "\n".join(lines)


def _iso(dt: datetime, all_day: bool) -> str:
    if all_day:
        return dt.date().isoformat()
    return dt.isoformat()


def parse_calendar(data: bytes) -> icalendar.Calendar:
    return icalendar.Calendar.from_ical(data)


def _coerce_dt(value: Any) -> tuple[datetime, bool]:
    """Return (aware-datetime, all_day-flag)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value, False
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc), True
    raise TypeError(f"unsupported DTSTART/DTEND value: {value!r}")


def _str(component: icalendar.Component, key: str) -> str:
    val = component.get(key)
    if val is None:
        return ""
    return str(val)


def _attendees(component: icalendar.Component) -> tuple[str, ...]:
    raw = component.get("attendee")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raw = [raw]
    return tuple(str(a) for a in raw)


def _categories(component: icalendar.Component) -> tuple[str, ...]:
    raw = component.get("categories")
    if raw is None:
        return ()
    cats = getattr(raw, "cats", None)
    if cats is not None:
        return tuple(str(c) for c in cats)
    return (str(raw),)


def to_event(component: icalendar.Component) -> Event:
    dtstart_field = component.get("dtstart")
    dtend_field = component.get("dtend") or component.get("dtstart")
    start_raw = dtstart_field.dt if dtstart_field is not None else None
    end_raw = dtend_field.dt if dtend_field is not None else None
    if start_raw is None or end_raw is None:
        raise ValueError("event missing DTSTART")
    start, all_day = _coerce_dt(start_raw)
    end, _ = _coerce_dt(end_raw)
    rec_id = component.get("recurrence-id")
    return Event(
        uid=_str(component, "uid"),
        summary=_str(component, "summary"),
        start=start,
        end=end,
        all_day=all_day,
        description=_str(component, "description"),
        location=_str(component, "location"),
        organizer=_str(component, "organizer"),
        attendees=_attendees(component),
        status=_str(component, "status"),
        url=_str(component, "url"),
        categories=_categories(component),
        recurrence_id=str(rec_id.dt) if rec_id is not None else "",
    )


def expand_events(cal: icalendar.Calendar, start: datetime, end: datetime) -> list[Event]:
    """Expand recurrences and return events in [start, end)."""
    occurrences = recurring_ical_events.of(cal).between(start, end)
    out: list[Event] = []
    for comp in occurrences:
        try:
            out.append(to_event(comp))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda e: e.start)
    return out


def find_master(cal: icalendar.Calendar, uid: str) -> Event | None:
    for comp in cal.walk("VEVENT"):
        if _str(comp, "uid") == uid and "recurrence-id" not in comp:
            try:
                return to_event(comp)
            except (ValueError, TypeError):
                return None
    # Fall back to the first match if no clear master.
    for comp in cal.walk("VEVENT"):
        if _str(comp, "uid") == uid:
            try:
                return to_event(comp)
            except (ValueError, TypeError):
                return None
    return None


__all__ = ["Event", "parse_calendar", "to_event", "expand_events", "find_master"]
