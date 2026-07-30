"""The US1 operator journey: enable one source, sync it, browse it, and resync (T047)."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config, load_config
from aggregato.db.engine import transaction
from aggregato.db.schema import entries, metadata, provider_items, works
from aggregato.db.search import create_search_index
from aggregato.domain.clock import Clock
from aggregato.domain.enums import FetchMode, RunStatus
from aggregato.main import create_app
from aggregato.sync.dispatch import run_once

TOKEN = "single-provider-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FIXTURE = (Path(__file__).parent.parent / "fixtures/fixture/log-two-pages.jsonl").resolve()


class FixedClock(Clock):
    """A deterministic run time for a child-process integration test."""

    def now(self) -> datetime:
        return datetime(2026, 7, 30, 12, tzinfo=UTC)


def _fts5_available() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE test_search USING fts5(content)")
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()
    return True


pytestmark = pytest.mark.skipif(not _fts5_available(), reason="the writer maintains an FTS5 index")


def config_for(data_dir: Path) -> Config:
    return load_config(
        {"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)},
        db_overrides={"providers": {"fixture": {"path": str(FIXTURE)}}},
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Config, AsyncEngine]]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = config_for(data_dir)
    app = create_app(config, run_migrations=False)
    async with app.state.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await create_search_index(conn)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http_client,
    ):
        http_client.headers.update(AUTH)
        yield http_client, config, app.state.engine


async def archive_counts(engine: AsyncEngine) -> tuple[int, int, int]:
    """Count the rows a duplicate resync must never create."""
    values: list[int] = []
    async with transaction(engine) as conn:
        for table in (works, provider_items, entries):
            values.append(
                int((await conn.execute(select(func.count()).select_from(table))).scalar_one())
            )
    return values[0], values[1], values[2]


async def test_single_provider_operator_journey(
    client: tuple[httpx.AsyncClient, Config, AsyncEngine],
) -> None:
    """A fresh archive becomes browsable, and a second run has no duplicate writes."""
    http_client, config, engine = client
    enabled = await http_client.post("/api/v1/providers/fixture/enable")
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    first = await run_once(
        engine,
        config,
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=FixedClock(),
    )
    assert first.status is RunStatus.SUCCESS, first.error_message
    browsed = await http_client.get("/api/v1/entries?include_subunits=true")
    assert browsed.status_code == 200
    assert len(browsed.json()["items"]) == 5
    before = await archive_counts(engine)

    second = await run_once(
        engine,
        config,
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=FixedClock(),
    )
    assert second.status is RunStatus.SUCCESS, second.error_message
    after = await archive_counts(engine)
    assert before == after == (5, 5, 5)
