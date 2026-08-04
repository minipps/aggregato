"""The injectable time source.

testing guidance makes time injectable rather than ambient so the scheduler and the retry ladder
can be tested without sleeping. Everything that needs "now" takes a ``Clock``; nothing calls
``datetime.now`` directly outside this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Reads the current instant. Always timezone-aware UTC (data-model.md §preamble)."""

    def now(self) -> datetime: ...


class SystemClock:
    """The real clock. The only place in the package that reads wall-clock time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


SYSTEM_CLOCK = SystemClock()
