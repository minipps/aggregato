"""The test infrastructure itself (T004).

Both fixtures below are constitutional requirements rather than conveniences, so they get a test:
a socket blocker that silently stopped blocking would let a provider's tests start hitting the real
platform, and every "no network in tests" claim in the design would quietly become false.
"""

from __future__ import annotations

import socket
from datetime import timedelta

import pytest

from tests.conftest import FROZEN_NOW, FrozenClock, NetworkAccessInTest


def test_outbound_connection_is_blocked() -> None:
    with pytest.raises(NetworkAccessInTest):
        socket.create_connection(("example.invalid", 80), 1)


def test_name_resolution_is_blocked() -> None:
    # Blocked too: a DNS lookup is an outbound request even when no connection follows.
    with pytest.raises(NetworkAccessInTest):
        socket.getaddrinfo("example.invalid", 80)


def test_loopback_is_blocked_as_well() -> None:
    # An in-process ASGI transport needs no socket. A test that binds one has stopped being
    # deterministic, so loopback gets no exemption.
    with pytest.raises(NetworkAccessInTest):
        socket.create_connection(("127.0.0.1", 8000), 1)


def test_clock_does_not_move_on_its_own(clock: FrozenClock) -> None:
    assert clock.now() == clock.now() == FROZEN_NOW


def test_clock_moves_only_when_a_test_moves_it(clock: FrozenClock) -> None:
    clock.advance(timedelta(minutes=5))
    assert clock.now() == FROZEN_NOW + timedelta(minutes=5)


def test_clock_is_timezone_aware(clock: FrozenClock) -> None:
    # Every timestamp in the system is UTC-aware (data-model.md); a naive clock would seed naive
    # values into the database through the scheduler.
    assert clock.now().tzinfo is not None
