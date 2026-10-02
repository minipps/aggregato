"""A fresh install makes exactly ZERO outbound requests .

One of the four permanently load-bearing tests in this project. It must never be marked skipped.

The claim is absolute, not "few" or "only metadata": install, start, browse, list providers — and
nothing reaches the network until an operator enables a platform themselves. That is what makes this
safe to run on a home network without reading the source first, and it is easy to lose by accident:
a provider module that fetches at import time, a registry that instantiates to read a declaration,
a health check that pings, an avatar loaded from a CDN.

`tests/conftest.py` blocks sockets process-wide, so a violation raises here rather than appearing
months later in a packet capture. This file also watches httpx2, catching a request before it
reaches a real transport.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from aggregato.config import Config, load_config
from aggregato.main import create_app
from aggregato.providers.registry import discover_providers, load_provider

TOKEN = "silent-install-token"


@pytest.fixture
def data_dir(tmp_path: Path) -> Iterator[Path]:
    directory = tmp_path / "data"
    directory.mkdir()
    yield directory


def config_for(data_dir: Path) -> Config:
    return load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)})


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any request dispatched to a REAL transport, and fail on it.

    Watching one layer below the socket catches a request that was built and handed to a transport,
    which is a stronger claim than "no packet left the host".

    Requests over an ``ASGITransport`` are exempt and delegate to the real implementation: that is
    the in-process app the test itself is driving, and it never touches a network. Without that
    exemption this fixture would flag the test's own calls and prove nothing.
    """
    attempts: list[str] = []
    original = httpx2.AsyncClient.send

    async def watched(
        self: httpx2.AsyncClient, request: httpx2.Request, **kwargs: object
    ) -> httpx2.Response:
        transport = self._transport_for_url(request.url)
        if isinstance(transport, httpx2.ASGITransport):
            return await original(self, request, **kwargs)  # type: ignore[arg-type]
        attempts.append(f"{request.method} {request.url}")
        raise AssertionError(
            f"a fresh install dispatched {request.method} {request.url} "
            f"to {type(transport).__name__}"
        )

    monkeypatch.setattr(httpx2.AsyncClient, "send", watched)
    return attempts


async def test_starting_the_service_sends_nothing(data_dir: Path, outbound: list[str]) -> None:
    app = create_app(config_for(data_dir))
    async with app.router.lifespan_context(app):
        pass
    assert outbound == []


async def test_browsing_a_fresh_install_sends_nothing(data_dir: Path, outbound: list[str]) -> None:
    """Authenticate, check health, list providers. All of it must stay local."""
    app = create_app(config_for(data_dir))

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            health = await client.get("/api/v1/health", headers=headers)
            listed = await client.get("/api/v1/providers", headers=headers)

    assert health.status_code == 200
    assert listed.status_code == 200
    assert outbound == []


def test_discovering_providers_runs_no_provider_code(outbound: list[str]) -> None:
    """Discovery reads declarations. Instantiating to read one is a request waiting to happen."""
    infos = discover_providers()
    assert infos, "discovery found nothing, so this test proves nothing"
    assert outbound == []


def test_importing_every_bundled_provider_sends_nothing(outbound: list[str]) -> None:
    """Import is the sneakiest path: a module-level fetch would fire before anything is enabled."""
    for info in discover_providers():
        load_provider(info.id)
    assert outbound == []


async def test_every_provider_starts_disabled(data_dir: Path, outbound: list[str]) -> None:
    """The property underneath the silence: nothing is due, because nothing is enabled."""
    app = create_app(config_for(data_dir))

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/providers", headers={"Authorization": f"Bearer {TOKEN}"}
            )

    listed = response.json()
    assert listed, "no providers discovered, so this test proves nothing"
    for provider in listed:
        assert provider["enabled"] is False, f"{provider['id']} ships enabled"
        assert provider["status"] == "disabled", f"{provider['id']} ships {provider['status']}"
    assert outbound == []


async def test_the_scheduler_finds_nothing_due_on_a_fresh_install(
    data_dir: Path, outbound: list[str]
) -> None:
    """The scheduler polls a fresh database and dispatches nothing."""
    import asyncio

    from aggregato.db.engine import create_engine
    from aggregato.db.migrate import upgrade_to_head
    from aggregato.sync.scheduler import due_providers

    config = config_for(data_dir)
    await asyncio.to_thread(upgrade_to_head, config.database_url)

    engine = create_engine(config.database_url)
    try:
        from datetime import UTC, datetime

        due = await due_providers(engine, now=datetime.now(UTC))
    finally:
        await engine.dispose()

    assert due == []
    assert outbound == []
