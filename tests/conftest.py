"""Shared test infrastructure.

Two fixtures here are constitutional infrastructure rather than convenience (Constitution II,
research.md R13): ``block_sockets`` makes network access in a test a hard failure, and ``clock``
is the injectable time source the scheduler and retry ladder take so no test sleeps.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aggregato.domain.clock import Clock

FROZEN_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class NetworkAccessInTest(RuntimeError):
    """Raised when a test opens a socket. A network call is a failure, not a slow test."""


@pytest.fixture(autouse=True)
def block_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that reaches the network.

    Autouse and unconditional: FR-036 and SC-010 require every test to run offline and without
    credentials, so this is not opt-in. Loopback is blocked too — an in-process ASGI transport
    needs no socket, and a test that binds one has stopped being deterministic.
    """

    def guard(*args: object, **kwargs: object) -> None:
        raise NetworkAccessInTest(
            "a test opened a socket; use recorded fixtures and httpx ASGITransport instead"
        )

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)


class FrozenClock(Clock):
    """A clock that only moves when a test moves it."""

    def __init__(self, start: datetime = FROZEN_NOW) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def data_dir(tmp_path: Path) -> Iterator[Path]:
    """An isolated ``$AGGREGATO_DATA`` for one test."""
    d = tmp_path / "data"
    d.mkdir()
    yield d
