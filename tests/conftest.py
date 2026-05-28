"""Shared fixtures."""

from __future__ import annotations

from textwrap import dedent

import pytest


@pytest.fixture
def simple_ics() -> bytes:
    return dedent(
        """\
        BEGIN:VCALENDAR
        VERSION:2.0
        PRODID:-//test//test//EN
        BEGIN:VEVENT
        UID:evt-1@test
        DTSTART:20260601T100000Z
        DTEND:20260601T110000Z
        SUMMARY:Standup
        DESCRIPTION:Daily sync
        LOCATION:Zoom
        CATEGORIES:work,meetings
        END:VEVENT
        BEGIN:VEVENT
        UID:evt-2@test
        DTSTART:20260602T140000Z
        DTEND:20260602T150000Z
        SUMMARY:Lunch with Alice
        LOCATION:Cafe Roma
        END:VEVENT
        BEGIN:VEVENT
        UID:evt-3@test
        DTSTART;VALUE=DATE:20260605
        DTEND;VALUE=DATE:20260606
        SUMMARY:Conference day
        END:VEVENT
        END:VCALENDAR
        """
    ).encode()


@pytest.fixture
def recurring_ics() -> bytes:
    return dedent(
        """\
        BEGIN:VCALENDAR
        VERSION:2.0
        PRODID:-//test//test//EN
        BEGIN:VEVENT
        UID:weekly@test
        DTSTART:20260601T090000Z
        DTEND:20260601T093000Z
        SUMMARY:Weekly sync
        RRULE:FREQ=WEEKLY;COUNT=4
        END:VEVENT
        END:VCALENDAR
        """
    ).encode()
