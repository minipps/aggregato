"""Durable progress is observable before a child process finishes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import create_engine, transaction
from aggregato.db.schema import metadata, sync_runs
from aggregato.domain.enums import RunPhase, RunStatus
from aggregato.domain.models import Cursor
from aggregato.sync.progress import RunProgress
from aggregato.sync.protocol import CheckpointMessage, FailureMessage


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


@pytest.fixture
async def engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/progress.db")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


async def test_child_messages_update_the_open_run_before_finalization(
    engine: AsyncEngine,
) -> None:
    started = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    async with transaction(engine) as conn:
        result = await conn.execute(
            sync_runs.insert().values(
                provider_id="fixture",
                lineage_id=uuid4(),
                attempt=1,
                mode="incremental",
                status=str(RunStatus.RUNNING),
                started_at=started,
            )
        )
        run_id = int(result.inserted_primary_key[0])

    clock = FixedClock(started)
    progress = RunProgress(engine=engine, run_id=run_id, clock=clock)
    await progress.set_phase(RunPhase.FETCHING)
    await progress.observe(FailureMessage(payload={"id": "bad"}, error="cannot normalize"))
    await progress.observe(CheckpointMessage(cursor=Cursor(state={"page": 2})))
    await progress.set_phase(RunPhase.INGESTING)
    await progress.record_ingest(items_written=0, items_failed=1)

    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(
                    sync_runs.c.phase,
                    sync_runs.c.items_seen,
                    sync_runs.c.items_written,
                    sync_runs.c.items_failed,
                    sync_runs.c.checkpoint_count,
                    sync_runs.c.cursor_after,
                    sync_runs.c.progress_revision,
                ).where(sync_runs.c.id == run_id)
            )
        ).one()

    assert row.phase == str(RunPhase.INGESTING)
    assert row.items_seen == 1
    assert row.items_written == 0
    assert row.items_failed == 1
    assert row.checkpoint_count == 1
    assert row.cursor_after == {"page": 2}
    assert row.progress_revision >= 4
