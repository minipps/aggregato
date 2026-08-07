"""Retention must not delete files belonging to queued import jobs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import create_engine, transaction
from aggregato.db.retention import cleanup
from aggregato.db.schema import import_jobs, metadata, providers
from aggregato.domain.enums import ProviderStatus

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_cleanup_preserves_queued_import_with_relative_data_dir(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = Path("data")
    path = data_dir / "imports" / "fixture" / "queued.csv"
    path.parent.mkdir(parents=True)
    path.write_text("id,title\n1,Queued\n", encoding="utf-8")

    async with transaction(engine) as conn:
        await conn.execute(
            providers.insert().values(
                id="fixture",
                enabled=True,
                status=str(ProviderStatus.IDLE),
                acquisition="export",
                schema_version=1,
                reviewed=True,
                config={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await conn.execute(
            import_jobs.insert().values(
                provider_id="fixture",
                path=str(path),
                created_at=NOW,
                attempts=0,
            )
        )

    await cleanup(engine, data_dir, now=NOW)

    assert path.is_file()
