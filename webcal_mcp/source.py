"""Calendar source abstraction.

Read-only today. Concrete write operations will land on subclasses that
flip the `writable` capability flag; consumers should branch on that
rather than on isinstance checks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .parser import Event


class CalendarSource(ABC):
    """A single named calendar."""

    name: str

    @property
    def writable(self) -> bool:
        return False

    @abstractmethod
    async def events(self, start: datetime, end: datetime) -> list[Event]:
        """Return events occurring within [start, end), recurrences expanded."""

    @abstractmethod
    async def get_event(self, uid: str) -> Event | None:
        """Return the master VEVENT for `uid`, or None if not present."""

    async def aclose(self) -> None:
        """Release any held resources."""
