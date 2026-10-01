"""The whole Phase 2 loop, once, for real (–).

Schedule → claim → spawn a child process → fetch → checkpoint → normalize → validate → ingest →
reschedule, against a real SQLite database and the real bundled provider. No mocks of our own code,
which is what testing guidance asks of an integration test.

This is not a user story test (those are 's,  onward). It exists because the pieces were
built separately, and "each part passes its own tests" is not evidence that they fit together.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine, transaction
from aggregato.db.schema import (
    entries,
    external_ids,
    import_jobs,
    ingest_failures,
    metadata,
    opinions,
    provider_items,
    provider_state,
    providers,
    replay_jobs,
    sync_runs,
    works,
)
from aggregato.db.search import create_search_index
from aggregato.domain.enums import ErrorClass, FetchMode, IngestStage, ProviderStatus, RunStatus
from aggregato.providers.manifest import BUNDLED_MANIFESTS
from aggregato.sync.dispatch import (
    _open_run,
    _reschedule,
    build_dispatch,
    run_once,
)
from aggregato.sync.jobs import ImportLease, claim_import, claim_replay, finish_import
from aggregato.sync.runner import RunOutcome
from aggregato.sync.scheduler import (
    DEFAULT_MAX_CONCURRENT_RUNS,
    SQLITE_MAX_CONCURRENT_RUNS,
    Scheduler,
    claim,
    due_providers,
    max_concurrent_runs,
    recover_interrupted_runs,
)

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
    """A clock a test moves by hand (testing guidance)."""

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


async def enable_provider(
    engine: AsyncEngine, provider_id: str, *, interval_seconds: int = 3600
) -> None:
    """Enable a provider the way the API's enable endpoint will."""
    async with transaction(engine) as conn:
        await conn.execute(
            providers.insert().values(
                id=provider_id,
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
                provider_id=provider_id,
                next_run_at=NOW,
                effective_interval_seconds=interval_seconds,
                consecutive_failures=0,
                retry_step=0,
                kv={},
            )
        )


async def enable_fixture(engine: AsyncEngine, *, interval_seconds: int = 3600) -> None:
    """Enable the fixture provider the way the API's enable endpoint will."""
    await enable_provider(engine, "fixture", interval_seconds=interval_seconds)


async def count(engine: AsyncEngine, table: object) -> int:
    async with transaction(engine) as conn:
        result = await conn.execute(select(func.count()).select_from(table))
        return int(result.scalar_one())


# --- The due queue IS the schedule -------------------------------------------------------------


async def test_a_newly_enabled_provider_is_immediately_due(engine: AsyncEngine) -> None:
    """Enable writes an explicit due time; a NULL schedule remains unscheduled."""
    await enable_fixture(engine)
    due = await due_providers(engine, now=NOW)
    assert [d.provider_id for d in due] == ["fixture"]


async def test_a_disabled_provider_is_never_due(engine: AsyncEngine) -> None:
    """A fresh install makes zero outbound requests, and this is where that starts ."""
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


async def test_worker_startup_recovers_an_interrupted_sync(engine: AsyncEngine) -> None:
    """A restarted container must not leave a provider permanently locked as syncing."""
    await enable_fixture(engine)
    await claim(engine, "fixture", now=NOW)
    async with transaction(engine) as conn:
        await conn.execute(
            sync_runs.insert().values(
                provider_id="fixture",
                lineage_id=uuid.uuid4(),
                attempt=1,
                mode="incremental",
                status=str(RunStatus.RUNNING),
                started_at=NOW,
                items_seen=0,
                items_written=0,
                items_failed=0,
            )
        )

    recovered = await recover_interrupted_runs(engine, now=NOW + timedelta(minutes=1))

    assert recovered == ["fixture"]
    async with transaction(engine) as conn:
        run = (
            (await conn.execute(select(sync_runs).where(sync_runs.c.provider_id == "fixture")))
            .mappings()
            .one()
        )
        provider = (
            (await conn.execute(select(providers).where(providers.c.id == "fixture")))
            .mappings()
            .one()
        )
        state = (
            (
                await conn.execute(
                    select(provider_state).where(provider_state.c.provider_id == "fixture")
                )
            )
            .mappings()
            .one()
        )
    assert run["status"] == str(RunStatus.FAILED)
    assert run["error_class"] == str(ErrorClass.INTERNAL)
    assert "will be retried" in run["error_message"]
    assert provider["status"] == str(ProviderStatus.IDLE)
    restart_at = NOW + timedelta(minutes=1)
    assert state["next_run_at"] == restart_at.replace(tzinfo=None)
    due = await due_providers(engine, now=restart_at)
    assert [provider.provider_id for provider in due] == ["fixture"]


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
    # them (5 + 2 + 3 + 2 + 1), every one of which must be stored .
    assert await count(engine, opinions) == 3
    assert await count(engine, external_ids) == 13


async def test_a_requested_full_mode_is_honoured_once_and_then_consumed(
    engine: AsyncEngine,
) -> None:
    """An operator's "full resync" must reach the provider, and must not become permanent.

    Two failures live here. The mode has to survive the hop from the API — which cannot spawn a run
    — to whichever dispatch picks the provider up, or it is silently downgraded to the scheduler's
    incremental default. And it has to be consumed, or one click would re-walk the platform's whole
    history on every poll from then on, including on each rung of the retry ladder.
    """
    await enable_fixture(engine)
    lineage = uuid.uuid4()
    async with transaction(engine) as conn:
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "fixture")
            .values(requested_mode="full", requested_lineage_id=lineage)
        )

    dispatch = build_dispatch(engine, config_for(FIXTURE), clock=StepClock())
    (due,) = await due_providers(engine, now=NOW)
    await dispatch(due)

    async with transaction(engine) as conn:
        run = (await conn.execute(select(sync_runs.c.mode, sync_runs.c.lineage_id))).one()
        left = (await conn.execute(select(provider_state.c.requested_mode))).scalar_one()
    assert run.mode == str(FetchMode.FULL)
    assert uuid.UUID(str(run.lineage_id)) == lineage
    assert left is None

    # The next scheduled poll is an ordinary incremental run again.
    (due,) = await due_providers(engine, now=NOW + timedelta(hours=2))
    await dispatch(due)
    async with transaction(engine) as conn:
        modes = [row.mode for row in await conn.execute(select(sync_runs.c.mode))]
    assert modes == [str(FetchMode.FULL), str(FetchMode.INCREMENTAL)]


async def test_a_stale_requested_run_keeps_the_new_request_and_inserts_no_run(
    engine: AsyncEngine,
) -> None:
    """A failed admission cannot consume a request before its run exists."""
    await enable_fixture(engine)
    stored_lineage = uuid.uuid4()
    stale_lineage = uuid.uuid4()
    async with transaction(engine) as conn:
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "fixture")
            .values(requested_mode="full", requested_lineage_id=stored_lineage)
        )

    with pytest.raises(RuntimeError, match="changed before run creation"):
        await _open_run(
            engine,
            provider_id="fixture",
            lineage_id=stale_lineage,
            attempt=1,
            mode=FetchMode.FULL,
            cursor_before=None,
            now=NOW,
            requested_mode="full",
            requested_lineage_id=stale_lineage,
        )

    async with transaction(engine) as conn:
        state = (
            await conn.execute(
                select(provider_state.c.requested_mode, provider_state.c.requested_lineage_id)
            )
        ).one()
        run_count = (await conn.execute(select(func.count()).select_from(sync_runs))).scalar_one()
    assert state.requested_mode == "full"
    assert uuid.UUID(str(state.requested_lineage_id)) == stored_lineage
    assert run_count == 0


async def test_import_priority_preserves_a_pending_requested_lineage(engine: AsyncEngine) -> None:
    await enable_fixture(engine)
    requested_lineage = uuid.uuid4()
    import_lineage = uuid.uuid4()
    async with transaction(engine) as conn:
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "fixture")
            .values(requested_mode="full", requested_lineage_id=requested_lineage)
        )

    run_id = await _open_run(
        engine,
        provider_id="fixture",
        lineage_id=import_lineage,
        attempt=1,
        mode=FetchMode.IMPORT,
        cursor_before=None,
        now=NOW,
    )
    await _reschedule(
        engine,
        provider_id="fixture",
        run_id=run_id,
        outcome=RunOutcome(status=RunStatus.SUCCESS),
        written=0,
        failed=0,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        lineage_id=import_lineage,
        clock=StepClock(),
        preserve_requested_request=True,
        requested_lineage_id=requested_lineage,
    )

    async with transaction(engine) as conn:
        state = (
            await conn.execute(
                select(provider_state.c.requested_mode, provider_state.c.requested_lineage_id)
            )
        ).one()
    assert state.requested_mode == str(FetchMode.FULL)
    assert uuid.UUID(str(state.requested_lineage_id)) == requested_lineage


async def test_import_with_null_lease_expiry_is_reclaimed(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    await enable_fixture(engine)
    path = tmp_path / "queued.csv"
    path.write_text("id,title\n1,Queued\n", encoding="utf-8")
    async with transaction(engine) as conn:
        await conn.execute(
            import_jobs.insert().values(
                provider_id="fixture",
                path=str(path),
                created_at=NOW,
                started_at=NOW - timedelta(minutes=20),
                lease_owner="dead-worker",
                lease_expires_at=None,
                attempts=1,
            )
        )

    job = await claim_import(engine, "fixture", now=NOW)

    assert job is not None
    assert job.path == path
    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(
                    import_jobs.c.lease_owner,
                    import_jobs.c.lease_expires_at,
                    import_jobs.c.attempts,
                )
            )
        ).one()
    assert row.lease_owner == job.owner
    assert row.lease_expires_at == (NOW + timedelta(minutes=15)).replace(tzinfo=None)
    assert row.attempts == 2

    stale = ImportLease(
        job_id=job.job_id,
        path=job.path,
        lineage_id=job.lineage_id,
        owner="stale-worker",
    )
    assert not await finish_import(engine, stale, now=NOW)
    async with transaction(engine) as conn:
        row = (await conn.execute(select(import_jobs.c.finished_at))).one()
    assert row.finished_at is None
    assert path.is_file()


async def test_direct_run_cannot_open_a_second_running_row(engine: AsyncEngine) -> None:
    await enable_fixture(engine)
    first_id = await _open_run(
        engine,
        provider_id="fixture",
        lineage_id=uuid.uuid4(),
        attempt=1,
        mode=FetchMode.INCREMENTAL,
        cursor_before=None,
        now=NOW,
    )

    with pytest.raises(RuntimeError, match="already has a running sync"):
        await _open_run(
            engine,
            provider_id="fixture",
            lineage_id=uuid.uuid4(),
            attempt=1,
            mode=FetchMode.INCREMENTAL,
            cursor_before=None,
            now=NOW,
        )

    async with transaction(engine) as conn:
        run_ids = [row.id for row in await conn.execute(select(sync_runs.c.id))]
    assert run_ids == [first_id]


async def test_host_failure_after_run_creation_is_finalized(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await enable_fixture(engine)

    def fail_provider_info(*_: object) -> object:
        raise RuntimeError("provider metadata unavailable")

    monkeypatch.setattr("aggregato.sync.dispatch._provider_info", fail_provider_info)
    outcome = await run_once(
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

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_class is ErrorClass.INTERNAL
    async with transaction(engine) as conn:
        run = (await conn.execute(select(sync_runs))).one()
        provider = (await conn.execute(select(providers))).one()
        state = (await conn.execute(select(provider_state))).one()
    assert run.status == str(RunStatus.FAILED)
    assert run.error_class == str(ErrorClass.INTERNAL)
    assert "provider metadata unavailable" in run.error_message
    assert provider.status == str(ProviderStatus.IDLE)
    assert state.retry_step == 1
    assert state.consecutive_failures == 1
    assert state.next_run_at == (NOW + timedelta(minutes=1)).replace(tzinfo=None)


async def test_replay_with_null_lease_expiry_is_reclaimed(engine: AsyncEngine) -> None:
    await enable_fixture(engine)
    lineage = uuid.uuid4()
    async with transaction(engine) as conn:
        run_result = await conn.execute(
            sync_runs.insert()
            .values(
                provider_id="fixture",
                lineage_id=lineage,
                attempt=1,
                mode=str(FetchMode.INCREMENTAL),
                status=str(RunStatus.SUCCESS),
                started_at=NOW,
            )
            .returning(sync_runs.c.id)
        )
        run_id = int(run_result.scalar_one())
        failure_result = await conn.execute(
            ingest_failures.insert()
            .values(
                provider_id="fixture",
                sync_run_id=run_id,
                native_id="native-1",
                raw_payload={"title": "Queued"},
                error="normalization failed",
                stage=str(IngestStage.NORMALIZE),
                created_at=NOW,
            )
            .returning(ingest_failures.c.id)
        )
        failure_id = int(failure_result.scalar_one())
        await conn.execute(
            replay_jobs.insert().values(
                failure_id=failure_id,
                provider_id="fixture",
                lineage_id=uuid.uuid4(),
                created_at=NOW,
                claimed_at=NOW - timedelta(minutes=20),
                lease_owner="dead-worker",
                lease_expires_at=None,
            )
        )

    job = await claim_replay(engine, "fixture", now=NOW)

    assert job is not None
    async with transaction(engine) as conn:
        row = (
            await conn.execute(select(replay_jobs.c.lease_owner, replay_jobs.c.lease_expires_at))
        ).one()
    assert row.lease_owner == job.owner
    assert row.lease_expires_at == (NOW + timedelta(minutes=15)).replace(tzinfo=None)


async def test_schema_bump_replays_retained_payloads_before_the_next_sync(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normalizer fix is applied to retained raw data without a special database command."""
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

    # The subprocess used for normalization need not know this test-only bump: replay's important
    # boundary is that it receives stored raws and the parent records the declared new version.
    monkeypatch.setitem(BUNDLED_MANIFESTS["fixture"], "schema_version", 2)
    outcome = await run_once(
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

    assert outcome.status is RunStatus.SUCCESS, outcome.error_message
    async with transaction(engine) as conn:
        stored_maximum = (
            await conn.execute(select(func.max(provider_items.c.schema_version)))
        ).scalar_one()
        provider_version = (
            await conn.execute(
                select(providers.c.schema_version).where(providers.c.id == "fixture")
            )
        ).scalar_one()
    assert stored_maximum == 2
    assert provider_version == 2


async def test_the_run_is_recorded_with_counts(engine: AsyncEngine) -> None:
    """A run that leaves no trace is the failure operators cannot diagnose ."""
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


# --- Resync writes nothing new  ------------------------------------------------


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
    """A broken source must look broken, never like an emptied history ."""
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


def test_sqlite_runs_are_serialized_but_server_databases_keep_parallelism() -> None:
    """A post-restart backlog must not overlap SQLite writers."""
    assert (
        max_concurrent_runs("sqlite+aiosqlite:///data/aggregato.db") == SQLITE_MAX_CONCURRENT_RUNS
    )
    assert max_concurrent_runs("postgresql+asyncpg://user:secret@db/aggregato") == (
        DEFAULT_MAX_CONCURRENT_RUNS
    )


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


async def test_the_scheduler_does_not_claim_beyond_available_slots(
    engine: AsyncEngine,
) -> None:
    """A queued provider stays idle until SQLite has a slot for its run."""
    # Scheduler admission uses the static manifests, so exercise two real poll-capable providers
    # even though dispatch is faked here to hold the SQLite slot open.
    await enable_provider(engine, "fixture")
    await enable_provider(engine, "letterboxd")
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    dispatched: list[str] = []

    async def blocking_dispatch(due: object) -> None:
        dispatched.append(due.provider_id)  # type: ignore[attr-defined]
        dispatch_started.set()
        await release_dispatch.wait()

    scheduler = Scheduler(
        engine,
        dispatch=blocking_dispatch,
        clock=StepClock(),
        max_concurrent=SQLITE_MAX_CONCURRENT_RUNS,
    )
    try:
        assert await scheduler.poll_once() == 1
        await dispatch_started.wait()

        # The second due provider must not be claimed while the one SQLite run slot is occupied.
        assert await scheduler.poll_once() == 0
        async with transaction(engine) as conn:
            rows = (await conn.execute(select(providers.c.id, providers.c.status))).all()
            statuses = {str(row.id): str(row.status) for row in rows}
        claimed_provider = dispatched[0]
        waiting_provider = "letterboxd" if claimed_provider == "fixture" else "fixture"
        assert statuses[claimed_provider] == str(ProviderStatus.SYNCING)
        assert statuses[waiting_provider] == str(ProviderStatus.IDLE)
    finally:
        release_dispatch.set()
        await scheduler.stop()

    assert len(dispatched) == 1


async def test_a_dispatch_that_raises_does_not_kill_the_loop(engine: AsyncEngine) -> None:
    """One bad provider must never stop the others ."""
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
