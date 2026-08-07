"""Request-scoped access to the application's injectable clock."""

from __future__ import annotations

from datetime import datetime

from starlette.requests import Request

from aggregato.domain.clock import SYSTEM_CLOCK


def now(request: Request) -> datetime:
    """Return the app's current UTC instant, with the system clock as a direct-app fallback."""
    clock = getattr(request.app.state, "clock", SYSTEM_CLOCK)
    return clock.now()
