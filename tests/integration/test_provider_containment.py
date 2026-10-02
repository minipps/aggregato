"""A failed provider run cannot take the API or another provider down ."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from aggregato.config import load_config
from aggregato.db.schema import metadata, provider_state, providers
from aggregato.db.search import create_search_index
from aggregato.domain.enums import ErrorClass, FetchMode, ProviderStatus, RunStatus
from aggregato.main import create_app
from aggregato.sync.dispatch import run_once
from aggregato.sync.runner import RunOutcome, RunRequest
from tests.conftest import FrozenClock

NOW = datetime(2026, 3, 1, tzinfo=UTC)


async def test_failed_child_is_contained_while_another_provider_and_the_api_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject the runner boundary; supervision itself has real-child unit coverage."""
    config = load_config(
        {"AGGREGATO_TOKEN": "contained", "AGGREGATO_DATA": str(tmp_path)},
        db_overrides={"database_url": f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"},
    )
    app = create_app(config, run_migrations=False)
    async with app.state.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await create_search_index(conn)
        for provider_id in ("fixture", "listenbrainz"):
            await conn.execute(
                providers.insert().values(
                    id=provider_id,
                    enabled=True,
                    status="idle",
                    acquisition="export",
                    schema_version=1,
                    reviewed=True,
                    config={},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await conn.execute(
                provider_state.insert().values(
                    provider_id=provider_id,
                    effective_interval_seconds=3600,
                    consecutive_failures=0,
                    retry_step=0,
                    kv={},
                )
            )

    async def fake_execute(request: RunRequest, **kwargs: object) -> RunOutcome:
        del kwargs
        if request.provider_id == "fixture":
            return RunOutcome(
                RunStatus.FAILED, error_class=ErrorClass.INTERNAL, error_message="SIGKILL"
            )
        return RunOutcome(RunStatus.SUCCESS)

    monkeypatch.setattr("aggregato.sync.dispatch.execute_run", fake_execute)
    clock = FrozenClock(NOW)
    failed, succeeded = await asyncio.gather(
        run_once(
            app.state.engine,
            config,
            provider_id="fixture",
            mode=FetchMode.INCREMENTAL,
            cursor=None,
            retry_step=0,
            consecutive_failures=0,
            interval_seconds=3600,
            clock=clock,
        ),
        run_once(
            app.state.engine,
            config,
            provider_id="listenbrainz",
            mode=FetchMode.INCREMENTAL,
            cursor=None,
            retry_step=0,
            consecutive_failures=0,
            interval_seconds=3600,
            clock=clock,
        ),
    )
    assert failed.status is RunStatus.FAILED
    assert succeeded.status is RunStatus.SUCCESS

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/health", headers={"Authorization": "Bearer contained"})
    assert response.status_code == 200
    by_id = {provider["id"]: provider for provider in response.json()["providers"]}
    assert by_id["fixture"]["status"] == ProviderStatus.IDLE
    assert by_id["listenbrainz"]["status"] == ProviderStatus.IDLE
    await app.state.engine.dispose()
