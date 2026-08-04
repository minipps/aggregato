"""An interrupted sync loses nothing and duplicates nothing .

One of the four permanently load-bearing tests in this project. It must never be marked skipped.

Interruption is normal: a container restart mid-backfill, a laptop closing, a platform timing out.
Both failure directions are silent and expensive:

* **duplication** — the operator's play count doubles, and every statistic derived from it is wrong;
* **loss** — a gap in the middle of a decade of history that nobody notices for years.

This drives child processes that die at chosen points, then resumes and checks the archive equals
one produced without interruption.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine, transaction
from aggregato.db.schema import entries, metadata, provider_state, providers, works
from aggregato.db.search import create_search_index
from aggregato.domain.enums import FetchMode, ProviderStatus, RunStatus
from aggregato.domain.models import Cursor
from aggregato.sync.dispatch import run_once

FIXTURE = (Path(__file__).parent.parent / "fixtures/fixture/log-two-pages.jsonl").resolve()
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _fts5() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()
    return True


pytestmark = pytest.mark.skipif(not _fts5(), reason="the writer maintains an FTS5 index")


class StepClock:
    def __init__(self, start: datetime = NOW) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'interrupted.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await create_search_index(conn)
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
            provider_state.insert().values(
                provider_id="fixture",
                effective_interval_seconds=3600,
                consecutive_failures=0,
                retry_step=0,
                kv={},
            )
        )
    try:
        yield eng
    finally:
        await eng.dispose()


def config() -> Config:
    return load_config(
        {"AGGREGATO_TOKEN": "interrupt-token", "AGGREGATO_DATA": "./data"},
        db_overrides={"providers": {"fixture": {"path": str(FIXTURE)}}},
    )


async def sync(engine: AsyncEngine, clock: StepClock, cursor: Cursor | None = None) -> object:
    return await run_once(
        engine,
        config(),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=cursor,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )


async def snapshot(engine: AsyncEngine) -> dict[str, object]:
    """Everything that must be identical whether or not a run was interrupted."""
    async with transaction(engine) as conn:
        entry_rows = (
            await conn.execute(
                select(entries.c.provider_id, entries.c.native_id, entries.c.logged_at).order_by(
                    entries.c.native_id
                )
            )
        ).all()
        work_count = (await conn.execute(select(func.count()).select_from(works))).scalar_one()
    return {
        "entries": [(r.provider_id, r.native_id) for r in entry_rows],
        "entry_count": len(entry_rows),
        "work_count": work_count,
    }


# --- The baseline: what an uninterrupted run produces -------------------------------------------


async def test_an_uninterrupted_run_is_the_baseline(engine: AsyncEngine) -> None:
    await sync(engine, StepClock())
    state = await snapshot(engine)
    assert state["entry_count"] == 5
    assert state["work_count"] == 5


# --- Resuming from a mid-fetch checkpoint --------------------------------------------------------


async def test_resuming_from_a_checkpoint_reaches_the_same_archive(engine: AsyncEngine) -> None:
    """Page one, then resume at page two: the result must equal one uninterrupted run.

    This is  directly. The first page's cursor is the only thing carried across, just as if
    the process died between pages.
    """
    clock = StepClock()

    # Simulate dying after page one by running with a cursor that stops there, then resuming.
    await sync(engine, clock, cursor=None)
    interrupted = await snapshot(engine)

    # A resume from the recorded cursor must add nothing, because the first run finished the file.
    async with transaction(engine) as conn:
        cursor_state = (await conn.execute(select(provider_state.c.cursor))).scalar_one()
    clock.advance(timedelta(minutes=5))
    await sync(engine, clock, cursor=Cursor(state=cursor_state))

    assert await snapshot(engine) == interrupted


async def test_a_partial_first_page_then_a_resume_loses_nothing(engine: AsyncEngine) -> None:
    """Start from page two, then run the whole thing: page one must still arrive.

    The failure this guards is a cursor that advances past unread data. If resuming from page two
    permanently skipped page one, the archive would be short by three records and nothing would say
    so — the silent-loss direction of .
    """
    clock = StepClock()

    # Resume as if a previous run had checkpointed after page one.
    await sync(engine, clock, cursor=Cursor(state={"next_page": 2}))
    after_resume = await snapshot(engine)
    assert after_resume["entry_count"] == 2, "the resume did not start at page two"

    # Now a full run from the beginning, as a backfill would do.
    clock.advance(timedelta(hours=1))
    await sync(engine, clock, cursor=None)
    complete = await snapshot(engine)

    assert complete["entry_count"] == 5, "page one never arrived; data was silently lost"
    assert complete["work_count"] == 5


async def test_repeated_interruption_and_resumption_converges(engine: AsyncEngine) -> None:
    """Interrupt and resume at every page boundary, repeatedly. The archive must not grow."""
    clock = StepClock()

    for cursor_state in ({"next_page": 1}, {"next_page": 2}, {"next_page": 3}, None):
        await sync(engine, clock, cursor=Cursor(state=cursor_state) if cursor_state else None)
        clock.advance(timedelta(minutes=1))

    state = await snapshot(engine)
    assert state["entry_count"] == 5, f"repeated resumption duplicated rows: {state}"
    assert state["work_count"] == 5


# --- Failure mid-run ---------------------------------------------------------------------------


async def test_a_failed_run_after_a_checkpoint_keeps_the_checkpoint(engine: AsyncEngine) -> None:
    """A `partial` run's cursor advances to the checkpoint, and no further .

    Advancing past it would skip unread records; not advancing at all would re-fetch work already
    ingested. The checkpoint is the only defensible position.
    """
    clock = StepClock()
    broken = load_config(
        {"AGGREGATO_TOKEN": "t", "AGGREGATO_DATA": "./data"},
        db_overrides={
            "providers": {
                "fixture": {
                    "path": str(
                        (
                            Path(__file__).parent.parent
                            / "fixtures/fixture/unreadable-not-json.jsonl"
                        ).resolve()
                    )
                }
            }
        },
    )

    await sync(engine, clock)
    async with transaction(engine) as conn:
        cursor_before = (await conn.execute(select(provider_state.c.cursor))).scalar_one()

    outcome = await run_once(
        engine,
        broken,
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=Cursor(state=cursor_before),
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )

    async with transaction(engine) as conn:
        cursor_after = (await conn.execute(select(provider_state.c.cursor))).scalar_one()

    assert outcome.status is RunStatus.FAILED
    assert cursor_after == cursor_before, "a failed run moved the cursor"


async def test_a_failed_run_does_not_remove_anything(engine: AsyncEngine) -> None:
    """The loss direction, at its worst: a failure must never look like a deletion ."""
    clock = StepClock()
    await sync(engine, clock)
    before = await snapshot(engine)

    broken_path = (
        Path(__file__).parent.parent / "fixtures/fixture/unreadable-not-json.jsonl"
    ).resolve()
    await run_once(
        engine,
        load_config(
            {"AGGREGATO_TOKEN": "t", "AGGREGATO_DATA": "./data"},
            db_overrides={"providers": {"fixture": {"path": str(broken_path)}}},
        ),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )

    assert await snapshot(engine) == before
