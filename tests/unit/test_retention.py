"""Retention must not delete files belonging to queued import jobs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import create_engine, transaction
from aggregato.db.retention import cleanup, update_settings
from aggregato.db.schema import (
    import_jobs,
    ingest_failures,
    metadata,
    provider_items,
    providers,
    sync_runs,
)
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


async def test_retention_settings_insert_and_update_with_one_upsert(engine: AsyncEngine) -> None:
    first = await update_settings(engine, {"raw_payload_retention_days": 45}, now=NOW)
    second = await update_settings(
        engine, {"raw_payload_retention_days": 12}, now=NOW + timedelta(days=1)
    )

    assert first["raw_payload_retention_days"] == 45
    assert second["raw_payload_retention_days"] == 12


async def test_raw_payload_retention_only_removes_resolved_failures(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    old = NOW - timedelta(days=2)
    async with transaction(engine) as conn:
        await conn.execute(
            provider_items.insert().values(
                provider_id="fixture",
                native_id="replay-source",
                title_as_given="Replay source",
                raw_payload={"id": "replay-source"},
                schema_version=1,
                first_seen_at=old,
                last_seen_at=old,
            )
        )
        await conn.execute(
            sync_runs.insert().values(
                id=1,
                provider_id="fixture",
                lineage_id=UUID("00000000-0000-0000-0000-000000000001"),
                attempt=1,
                mode="incremental",
                status="success",
                started_at=NOW,
                finished_at=NOW,
            )
        )
        await conn.execute(
            ingest_failures.insert().values(
                id=1,
                provider_id="fixture",
                sync_run_id=1,
                raw_payload={"id": "resolved"},
                error="resolved",
                stage="normalize",
                created_at=old,
                resolved_at=old,
            )
        )
        await conn.execute(
            ingest_failures.insert().values(
                id=2,
                provider_id="fixture",
                sync_run_id=1,
                raw_payload={"id": "unresolved"},
                error="unresolved",
                stage="normalize",
                created_at=old,
            )
        )
    await update_settings(engine, {"raw_payload_retention_days": 1}, now=NOW)

    await cleanup(engine, tmp_path, now=NOW)

    async with transaction(engine) as conn:
        failure_ids = list((await conn.scalars(select(ingest_failures.c.id))).all())
        replay_payload = await conn.scalar(select(provider_items.c.raw_payload))
    assert failure_ids == [2]
    assert replay_payload == {"id": "replay-source"}
