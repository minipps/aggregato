"""The injectable time source.

The scheduler and retry logic take a ``Clock`` so tests control time without sleeping. Other code
uses this interface rather than calling ``datetime.now`` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Reads the current instant as a timezone-aware UTC value."""

    def now(self) -> datetime: ...


class SystemClock:
    """The real clock. The only place in the package that reads wall-clock time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


SYSTEM_CLOCK = SystemClock()
