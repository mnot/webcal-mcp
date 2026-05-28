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
    async def events(self, start: datetime, end: datetime, *, refresh: bool = False) -> list[Event]:
        """Return events occurring within [start, end), recurrences expanded.

        If `refresh` is True, bypass the TTL cache and revalidate against
        the upstream source.
        """

    @abstractmethod
    async def get_event(self, uid: str, *, refresh: bool = False) -> Event | None:
        """Return the master VEVENT for `uid`, or None if not present.

        If `refresh` is True, bypass the TTL cache and revalidate.
        """

    async def aclose(self) -> None:
        """Release any held resources."""
