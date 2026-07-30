"""A provider that does not report deletions never erases history by default (T084)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine
from aggregato.db.schema import entries, metadata, provider_state, providers
from aggregato.db.search import create_search_index
from aggregato.domain.enums import FetchMode
from aggregato.sync.dispatch import run_once
from tests.conftest import FrozenClock

NOW = datetime(2026, 3, 1, tzinfo=UTC)
FIXTURE = (Path(__file__).parent.parent / "fixtures/fixture/log-two-pages.jsonl").resolve()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'deletes.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await create_search_index(conn)
        await conn.execute(
            providers.insert().values(
                id="fixture",
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
                provider_id="fixture",
                effective_interval_seconds=3600,
                consecutive_failures=0,
                retry_step=0,
                kv={},
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


def config(path: Path) -> Config:
    return load_config(
        {"AGGREGATO_TOKEN": "deletes", "AGGREGATO_DATA": "./data"},
        db_overrides={"providers": {"fixture": {"path": str(path)}}},
    )


async def test_a_provider_without_reports_deletes_never_inferrs_tombstones(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    clock = FrozenClock(NOW)
    await run_once(
        engine,
        config(FIXTURE),
        provider_id="fixture",
        mode=FetchMode.FULL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )
    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(entries))).scalar_one() == 5

    empty = tmp_path / "empty.jsonl"
    empty.write_text('{"page": 1, "records": []}\n', encoding="utf-8")
    clock.advance(timedelta(hours=1))
    await run_once(
        engine,
        config(empty),
        provider_id="fixture",
        mode=FetchMode.FULL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )

    async with engine.connect() as conn:
        # Fixture declares no reports_deletes and infer_deletes is unset (false), so an empty full
        # result is a silent archive-preserving no-op, not a mass tombstone.
        assert (
            await conn.execute(
                select(func.count()).select_from(entries).where(entries.c.deleted_at.is_(None))
            )
        ).scalar_one() == 5
