"""The whole Phase 2 loop, once, for real (T033–T039).

Schedule → claim → spawn a child process → fetch → checkpoint → normalize → validate → ingest →
reschedule, against a real SQLite database and the real bundled provider. No mocks of our own code,
which is what Constitution II asks of an integration test.

This is not a user story test (those are US1's, T047 onward). It exists because the pieces were
built separately, and "each part passes its own tests" is not evidence that they fit together.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine, transaction
from aggregato.db.schema import (
    entries,
    external_ids,
    metadata,
    opinions,
    provider_items,
    provider_state,
    providers,
    sync_runs,
    works,
)
from aggregato.db.search import create_search_index
from aggregato.domain.enums import ErrorClass, FetchMode, ProviderStatus, RunStatus
from aggregato.sync.dispatch import run_once
from aggregato.sync.scheduler import Scheduler, claim, due_providers

FIXTURE = (Path(__file__).parent.parent / "fixtures/fixture/log-two-pages.jsonl").resolve()
BROKEN = (Path(__file__).parent.parent / "fixtures/fixture/unreadable-not-json.jsonl").resolve()
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
    """A clock a test moves by hand (Constitution II)."""

    def __init__(self, start: datetime = NOW) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await create_search_index(conn)
    try:
        yield eng
    finally:
        await eng.dispose()


def config_for(path: Path) -> Config:
    return load_config(
        {"AGGREGATO_TOKEN": "test-token", "AGGREGATO_DATA": "./data"},
        db_overrides={"providers": {"fixture": {"path": str(path)}}},
    )


async def enable_fixture(engine: AsyncEngine, *, interval_seconds: int = 3600) -> None:
    """Enable the provider the way the API's enable endpoint will."""
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
            provider_state.insert().values(
                provider_id="fixture",
                effective_interval_seconds=interval_seconds,
                consecutive_failures=0,
                retry_step=0,
                kv={},
            )
        )


async def count(engine: AsyncEngine, table: object) -> int:
    async with transaction(engine) as conn:
        result = await conn.execute(select(func.count()).select_from(table))
        return int(result.scalar_one())


# --- The due queue IS the schedule -------------------------------------------------------------


async def test_a_newly_enabled_provider_is_immediately_due(engine: AsyncEngine) -> None:
    """``next_run_at IS NULL`` counts as due, so a first run needs no separate code path."""
    await enable_fixture(engine)
    due = await due_providers(engine, now=NOW)
    assert [d.provider_id for d in due] == ["fixture"]


async def test_a_disabled_provider_is_never_due(engine: AsyncEngine) -> None:
    """A fresh install makes zero outbound requests, and this is where that starts (SC-013)."""
    async with transaction(engine) as conn:
        await conn.execute(
            providers.insert().values(
                id="fixture",
                enabled=False,
                status=str(ProviderStatus.DISABLED),
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
    assert await due_providers(engine, now=NOW) == []


async def test_claiming_twice_fails_the_second_time(engine: AsyncEngine) -> None:
    """The claim is a SQL predicate, so it survives a restart and two overlapping polls."""
    await enable_fixture(engine)
    assert await claim(engine, "fixture", now=NOW) is True
    assert await claim(engine, "fixture", now=NOW) is False


async def test_a_syncing_provider_is_not_dispatched_again(engine: AsyncEngine) -> None:
    await enable_fixture(engine)
    await claim(engine, "fixture", now=NOW)
    assert await due_providers(engine, now=NOW) == []


# --- One full run ------------------------------------------------------------------------------


async def test_a_full_run_ingests_the_fixture(engine: AsyncEngine) -> None:
    await enable_fixture(engine)
    clock = StepClock()

    outcome = await run_once(
        engine,
        config_for(FIXTURE),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )

    assert outcome.status is RunStatus.SUCCESS, outcome.error_message
    assert await count(engine, works) == 5
    assert await count(engine, entries) == 5
    assert await count(engine, provider_items) == 5
    # Three records carry a rating or a review; the five records carry 13 identifiers between
    # them (5 + 2 + 3 + 2 + 1), every one of which must be stored (FR-009).
    assert await count(engine, opinions) == 3
    assert await count(engine, external_ids) == 13


async def test_the_run_is_recorded_with_counts(engine: AsyncEngine) -> None:
    """A run that leaves no trace is the failure operators cannot diagnose (FR-019)."""
    await enable_fixture(engine)
    await run_once(
        engine,
        config_for(FIXTURE),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=StepClock(),
    )

    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(
                    sync_runs.c.status,
                    sync_runs.c.items_seen,
                    sync_runs.c.items_written,
                    sync_runs.c.items_failed,
                    sync_runs.c.lineage_id,
                    sync_runs.c.attempt,
                    sync_runs.c.finished_at,
                    sync_runs.c.cursor_after,
                )
            )
        ).one()

    assert row.status == str(RunStatus.SUCCESS)
    assert row.items_seen == 5
    assert row.items_written == 5
    assert row.items_failed == 0
    assert row.attempt == 1
    assert row.finished_at is not None
    assert uuid.UUID(str(row.lineage_id))
    assert row.cursor_after == {"next_page": 3}


async def test_the_cursor_advances_to_the_last_checkpoint(engine: AsyncEngine) -> None:
    await enable_fixture(engine)
    await run_once(
        engine,
        config_for(FIXTURE),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=StepClock(),
    )

    async with transaction(engine) as conn:
        cursor = (await conn.execute(select(provider_state.c.cursor))).scalar_one()
    assert cursor == {"next_page": 3}


async def test_a_successful_run_reschedules_at_the_normal_interval(engine: AsyncEngine) -> None:
    await enable_fixture(engine, interval_seconds=3600)
    clock = StepClock()
    await run_once(
        engine,
        config_for(FIXTURE),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )

    async with transaction(engine) as conn:
        state = (
            await conn.execute(
                select(
                    provider_state.c.next_run_at,
                    provider_state.c.retry_step,
                    provider_state.c.consecutive_failures,
                    provider_state.c.last_success_at,
                )
            )
        ).one()
        status = (await conn.execute(select(providers.c.status))).scalar_one()

    assert status == str(ProviderStatus.IDLE)
    assert state.retry_step == 0
    assert state.consecutive_failures == 0
    assert state.last_success_at is not None
    assert state.next_run_at.replace(tzinfo=UTC) == NOW + timedelta(hours=1)


# --- Resync writes nothing new (FR-005, SC-003) ------------------------------------------------


async def test_a_second_identical_run_writes_nothing_new(engine: AsyncEngine) -> None:
    """The claim the whole design rests on, exercised through the real process boundary."""
    await enable_fixture(engine)
    config = config_for(FIXTURE)
    clock = StepClock()

    for _ in range(3):
        await run_once(
            engine,
            config,
            provider_id="fixture",
            mode=FetchMode.INCREMENTAL,
            cursor=None,
            retry_step=0,
            consecutive_failures=0,
            interval_seconds=3600,
            clock=clock,
        )
        clock.advance(timedelta(hours=1))

    assert await count(engine, works) == 5
    assert await count(engine, entries) == 5
    assert await count(engine, opinions) == 3
    assert await count(engine, external_ids) == 13
    # Three runs recorded, one archive's worth of data.
    assert await count(engine, sync_runs) == 3


# --- Failure does not destroy anything ---------------------------------------------------------


async def test_a_structure_change_fails_the_run_without_touching_the_archive(
    engine: AsyncEngine,
) -> None:
    """A broken source must look broken, never like an emptied history (FR-024, FR-026)."""
    await enable_fixture(engine)
    clock = StepClock()

    # First a good run, so there is an archive to endanger.
    await run_once(
        engine,
        config_for(FIXTURE),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )
    before = await count(engine, entries)

    outcome = await run_once(
        engine,
        config_for(BROKEN),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_class is ErrorClass.STRUCTURE_CHANGED
    assert await count(engine, entries) == before, "a failed run altered the archive"


async def test_structure_changed_goes_degraded_with_no_retry_scheduled(
    engine: AsyncEngine,
) -> None:
    """No amount of waiting fixes code that needs updating (contract §4)."""
    await enable_fixture(engine)
    await run_once(
        engine,
        config_for(BROKEN),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=StepClock(),
    )

    async with transaction(engine) as conn:
        status = (await conn.execute(select(providers.c.status))).scalar_one()
        next_run_at = (await conn.execute(select(provider_state.c.next_run_at))).scalar_one()

    assert status == str(ProviderStatus.DEGRADED)
    assert next_run_at is None, "a never-retry class scheduled a retry"


async def test_a_failed_run_leaves_the_cursor_alone(engine: AsyncEngine) -> None:
    """Cursor unchanged on failure, so work between the last checkpoint and the failure survives."""
    await enable_fixture(engine)
    clock = StepClock()
    await run_once(
        engine,
        config_for(FIXTURE),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )
    await run_once(
        engine,
        config_for(BROKEN),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )

    async with transaction(engine) as conn:
        cursor = (await conn.execute(select(provider_state.c.cursor))).scalar_one()
    assert cursor == {"next_page": 3}


# --- The scheduler loop dispatches ------------------------------------------------------------


async def test_the_scheduler_dispatches_a_due_provider(engine: AsyncEngine) -> None:
    await enable_fixture(engine)
    dispatched: list[str] = []

    async def fake_dispatch(due: object) -> None:
        dispatched.append(due.provider_id)  # type: ignore[attr-defined]

    scheduler = Scheduler(engine, dispatch=fake_dispatch, clock=StepClock())
    started = await scheduler.poll_once()
    await scheduler.stop()

    assert started == 1
    assert dispatched == ["fixture"]


async def test_a_dispatch_that_raises_does_not_kill_the_loop(engine: AsyncEngine) -> None:
    """One bad provider must never stop the others (FR-025)."""
    await enable_fixture(engine)

    async def exploding_dispatch(due: object) -> None:
        raise RuntimeError("provider host bug")

    scheduler = Scheduler(engine, dispatch=exploding_dispatch, clock=StepClock())
    await scheduler.poll_once()
    await scheduler.stop()

    async with transaction(engine) as conn:
        status = (await conn.execute(select(providers.c.status))).scalar_one()
        failures = (await conn.execute(select(provider_state.c.consecutive_failures))).scalar_one()

    # Recorded against the provider, and the loop is still usable.
    assert status == str(ProviderStatus.DEGRADED)
    assert failures == 1
    assert await scheduler.poll_once() == 0  # degraded but still scheduled, not due yet


async def test_the_scheduler_runs_the_real_dispatch_end_to_end(engine: AsyncEngine) -> None:
    """The loop, the child process, and the writer, with nothing faked."""
    from aggregato.sync.dispatch import build_dispatch

    await enable_fixture(engine)
    clock = StepClock()
    scheduler = Scheduler(
        engine, dispatch=build_dispatch(engine, config_for(FIXTURE), clock=clock), clock=clock
    )

    assert await scheduler.poll_once() == 1
    await scheduler.stop()

    assert await count(engine, entries) == 5
    async with transaction(engine) as conn:
        status = (await conn.execute(select(providers.c.status))).scalar_one()
    assert status == str(ProviderStatus.IDLE)
