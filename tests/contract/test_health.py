"""``GET /health`` matches the contract, and needs credentials like everything else (T043).

Shape per ``contracts/openapi.yaml``: ``{status: ok|degraded, providers: [{id, status,
last_success_at, consecutive_failures}]}``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.api.deps import register_auth, require_auth
from aggregato.api.errors import PROBLEM_MEDIA_TYPE, register_error_handlers
from aggregato.api.routes import health
from aggregato.config import load_config
from aggregato.db.engine import create_engine, transaction
from aggregato.db.schema import metadata, provider_state, providers
from aggregato.domain.enums import Acquisition, ProviderStatus

TOKEN = "correct-horse-battery-staple"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def engine(data_dir: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(f"sqlite+aiosqlite:///{data_dir}/aggregato.db")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def client(engine: AsyncEngine, data_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI(dependencies=[Depends(require_auth)])
    register_error_handlers(app)
    register_auth(
        app,
        config=load_config(env={"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)}),
        engine=engine,
    )
    app.include_router(health.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _add_provider(
    engine: AsyncEngine,
    provider_id: str,
    status: ProviderStatus,
    *,
    state: dict[str, Any] | None = None,
) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            providers.insert().values(
                id=provider_id,
                enabled=True,
                status=status.value,
                acquisition=Acquisition.API.value,
                schema_version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        if state is not None:
            await conn.execute(
                provider_state.insert().values(
                    provider_id=provider_id, effective_interval_seconds=3600, **state
                )
            )


async def test_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)


async def test_fresh_install_has_no_providers(client: httpx.AsyncClient) -> None:
    """Zero provider rows is a fresh install, not an error."""
    response = await client.get("/health", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "providers": []}


async def test_shape_matches_contract(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    await _add_provider(
        engine,
        "letterboxd",
        ProviderStatus.IDLE,
        state={"last_success_at": NOW, "consecutive_failures": 0},
    )
    # No provider_state row yet: still reported, with nulls rather than a 500.
    await _add_provider(engine, "listenbrainz", ProviderStatus.SYNCING)

    payload = (await client.get("/health", headers=AUTH)).json()
    assert payload["status"] == "ok"
    assert [p["id"] for p in payload["providers"]] == ["letterboxd", "listenbrainz"]
    first, second = payload["providers"]
    assert set(first) == {"id", "status", "last_success_at", "consecutive_failures"}
    assert first["status"] == "idle"
    assert first["consecutive_failures"] == 0
    # ISO 8601 with an offset, per the contract's conventions.
    assert datetime.fromisoformat(first["last_success_at"]) == NOW
    assert second["last_success_at"] is None
    assert second["consecutive_failures"] == 0


async def test_degraded_when_any_provider_is_degraded(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await _add_provider(engine, "letterboxd", ProviderStatus.IDLE)
    await _add_provider(
        engine,
        "trakt",
        ProviderStatus.DEGRADED,
        state={"consecutive_failures": 4},
    )

    payload = (await client.get("/health", headers=AUTH)).json()
    assert payload["status"] == "degraded"
    trakt = next(p for p in payload["providers"] if p["id"] == "trakt")
    assert trakt["status"] == "degraded"
    assert trakt["consecutive_failures"] == 4
