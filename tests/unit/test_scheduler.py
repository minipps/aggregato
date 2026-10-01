"""Admission and release invariants for the database-backed scheduler."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from aggregato.db.engine import transaction
from aggregato.db.schema import (
    import_jobs,
    ingest_failures,
    metadata,
    provider_state,
    providers,
    replay_jobs,
    sync_runs,
)
from aggregato.domain.enums import Capability, ErrorClass, FetchMode, ProviderStatus, RunStatus
from aggregato.providers.registry import ProviderInfo
from aggregato.sync import scheduler as scheduler_module
from aggregato.sync.retry import plan_after_failure
from aggregato.sync.scheduler import DueProvider, Scheduler, claim, due_providers, release
from tests.conftest import FrozenClock
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _info(provider_id: str, *, poll: bool) -> ProviderInfo:
    capabilities = {Capability.POLL.value} if poll else set()
    return ProviderInfo(
        id=provider_id,
        name=provider_id,
        module=f"tests.providers.{provider_id}",
        media_types=frozenset(),
        capabilities=frozenset(capabilities),
        acquisition="api",
        schema_version=1,
        default_poll_interval=timedelta(hours=1),
        reviewed=True,
        api_visible=True,
        config_schema={},
        rating_scales=(),
    )


class _SyncTransaction:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connection: Connection | None = None
        self._transaction: object | None = None

    async def __aenter__(self) -> SyncConnectionAdapter:
        self._connection = self._engine.connect()
        self._transaction = self._connection.begin()
        return SyncConnectionAdapter(self._connection)

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        assert self._connection is not None
        assert self._transaction is not None
        if exc_type is None:
            self._transaction.commit()  # type: ignore[attr-defined]
        else:
            self._transaction.rollback()  # type: ignore[attr-defined]
        self._connection.close()


class _SyncAsyncEngine:
    """Make the production transaction helper usable without the unavailable aiosqlite worker."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def begin(self) -> _SyncTransaction:
        return _SyncTransaction(self._engine)

    def dispose(self) -> None:
        self._engine.dispose()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[_SyncAsyncEngine]:
    sync_engine = create_sync_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    with sync_engine.begin() as conn:
        metadata.create_all(conn)
    engine = _SyncAsyncEngine(sync_engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def static_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give scheduler tests explicit metadata for every database row they create."""
    infos = [
        _info("healthy", poll=True),
        _info("disabled", poll=True),
        _info("misconfigured", poll=True),
        _info("future", poll=True),
        _info("non-poll", poll=False),
    ]
    monkeypatch.setattr(scheduler_module, "discover_providers", lambda: infos)


async def _insert_provider(
    engine: _SyncAsyncEngine,
    provider_id: str,
    *,
    enabled: bool = True,
    status: ProviderStatus = ProviderStatus.IDLE,
    next_run_at: datetime | None = NOW,
    requested_mode: str | None = None,
    requested_lineage_id: uuid.UUID | None = None,
) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            providers.insert().values(
                id=provider_id,
                enabled=enabled,
                status=str(status),
                acquisition="api",
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
                next_run_at=next_run_at,
                effective_interval_seconds=3600,
                consecutive_failures=0,
                retry_step=0,
                requested_mode=requested_mode,
                requested_lineage_id=requested_lineage_id,
                kv={},
            )
        )


async def _provider_and_state(engine: _SyncAsyncEngine, provider_id: str) -> tuple[object, object]:
    async with transaction(engine) as conn:
        provider = (
            await conn.execute(select(providers).where(providers.c.id == provider_id))
        ).one()
        state = (
            await conn.execute(
                select(provider_state).where(provider_state.c.provider_id == provider_id)
            )
        ).one()
    return provider, state


async def test_due_selection_excludes_disabled_misconfigured_and_non_poll_providers(
    engine: _SyncAsyncEngine, static_metadata: None
) -> None:
    lineage = uuid.uuid4()
    await _insert_provider(
        engine,
        "healthy",
        requested_mode="full",
        requested_lineage_id=lineage,
    )
    await _insert_provider(engine, "disabled", enabled=False, status=ProviderStatus.DISABLED)
    await _insert_provider(engine, "misconfigured", status=ProviderStatus.MISCONFIGURED)
    await _insert_provider(engine, "non-poll")

    due = await due_providers(engine, now=NOW)

    assert [item.provider_id for item in due] == ["healthy"]
    assert due[0].requested_mode == "full"
    assert due[0].requested_lineage_id == lineage


async def test_claim_revalidates_every_admission_predicate(
    engine: _SyncAsyncEngine, static_metadata: None
) -> None:
    await _insert_provider(engine, "healthy")
    await _insert_provider(engine, "disabled", enabled=False, status=ProviderStatus.DISABLED)
    await _insert_provider(engine, "misconfigured", status=ProviderStatus.MISCONFIGURED)
    await _insert_provider(engine, "future", next_run_at=NOW + timedelta(seconds=1))
    await _insert_provider(engine, "non-poll")

    results = await asyncio.gather(
        claim(engine, "disabled", now=NOW),
        claim(engine, "misconfigured", now=NOW),
        claim(engine, "future", now=NOW),
        claim(engine, "non-poll", now=NOW),
    )

    assert results == [False, False, False, False]
    provider, _ = await _provider_and_state(engine, "healthy")
    assert provider.status == str(ProviderStatus.IDLE)


async def test_non_poll_provider_with_a_queued_import_is_admitted(
    engine: _SyncAsyncEngine, static_metadata: None, tmp_path: Path
) -> None:
    await _insert_provider(
        engine,
        "non-poll",
        enabled=False,
        status=ProviderStatus.DISABLED,
        next_run_at=None,
    )
    async with transaction(engine) as conn:
        await conn.execute(
            import_jobs.insert().values(
                provider_id="non-poll",
                path=str(tmp_path / "queued.csv"),
                created_at=NOW,
            )
        )

    due = await due_providers(engine, now=NOW)

    assert [item.provider_id for item in due] == ["non-poll"]
    assert await claim(engine, "non-poll", now=NOW)


async def test_disabled_provider_with_a_queued_replay_is_admitted(
    engine: _SyncAsyncEngine, static_metadata: None
) -> None:
    await _insert_provider(
        engine,
        "non-poll",
        enabled=False,
        status=ProviderStatus.DISABLED,
        next_run_at=None,
    )
    async with transaction(engine) as conn:
        run_result = await conn.execute(
            sync_runs.insert()
            .values(
                provider_id="non-poll",
                lineage_id=uuid.uuid4(),
                attempt=1,
                mode="incremental",
                status=str(RunStatus.FAILED),
                started_at=NOW,
            )
            .returning(sync_runs.c.id)
        )
        failure_result = await conn.execute(
            ingest_failures.insert()
            .values(
                provider_id="non-poll",
                sync_run_id=int(run_result.scalar_one()),
                native_id="item-1",
                raw_payload={"item": 1},
                error="normalize failed",
                stage="normalize",
                created_at=NOW,
            )
            .returning(ingest_failures.c.id)
        )
        await conn.execute(
            replay_jobs.insert().values(
                failure_id=int(failure_result.scalar_one()),
                provider_id="non-poll",
                lineage_id=uuid.uuid4(),
                created_at=NOW,
            )
        )

    due = await due_providers(engine, now=NOW)

    assert [item.provider_id for item in due] == ["non-poll"]
    assert await claim(engine, "non-poll", now=NOW)


async def test_null_schedule_is_unscheduled_but_a_queued_check_is_still_admitted(
    engine: _SyncAsyncEngine, static_metadata: None
) -> None:
    await _insert_provider(engine, "healthy", next_run_at=None)
    await _insert_provider(
        engine,
        "disabled",
        enabled=False,
        status=ProviderStatus.DISABLED,
        next_run_at=None,
        requested_mode=str(FetchMode.CHECK),
    )

    due = await due_providers(engine, now=NOW)

    assert [item.provider_id for item in due] == ["disabled"]
    assert not await claim(engine, "healthy", now=NOW)
    assert await claim(engine, "disabled", now=NOW)


@pytest.mark.parametrize(
    "error_class", [ErrorClass.AUTH, ErrorClass.BLOCKED, ErrorClass.STRUCTURE_CHANGED]
)
async def test_never_retry_release_stays_unscheduled_until_manual_due_time(
    engine: _SyncAsyncEngine,
    static_metadata: None,
    error_class: ErrorClass,
) -> None:
    await _insert_provider(engine, "healthy", next_run_at=NOW)
    assert await claim(engine, "healthy", now=NOW)
    decision = plan_after_failure(
        clock=FrozenClock(NOW),
        error_class=error_class,
        retry_step=0,
        consecutive_failures=0,
        normal_interval=timedelta(hours=1),
        lineage_id=uuid.uuid4(),
    )
    await release(
        engine,
        "healthy",
        status=decision.status,
        next_run_at=decision.next_run_at,
        retry_step=decision.retry_step,
        consecutive_failures=decision.consecutive_failures,
        now=NOW,
    )

    assert await due_providers(engine, now=NOW) == []
    assert not await claim(engine, "healthy", now=NOW)

    async with transaction(engine) as conn:
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "healthy")
            .values(next_run_at=NOW, requested_mode=str(FetchMode.INCREMENTAL))
        )
    assert await claim(engine, "healthy", now=NOW)


async def test_release_refuses_to_overwrite_state_while_another_run_is_open(
    engine: _SyncAsyncEngine, static_metadata: None
) -> None:
    await _insert_provider(engine, "healthy", next_run_at=NOW)
    assert await claim(engine, "healthy", now=NOW)
    async with transaction(engine) as conn:
        first_result = await conn.execute(
            sync_runs.insert()
            .values(
                provider_id="healthy",
                lineage_id=uuid.uuid4(),
                attempt=1,
                mode="incremental",
                status=str(RunStatus.RUNNING),
                started_at=NOW,
            )
            .returning(sync_runs.c.id)
        )
        first_id = int(first_result.scalar_one())
        await conn.execute(
            sync_runs.insert().values(
                provider_id="healthy",
                lineage_id=uuid.uuid4(),
                attempt=1,
                mode="incremental",
                status=str(RunStatus.RUNNING),
                started_at=NOW,
            )
        )

    with pytest.raises(RuntimeError, match="another running sync"):
        await release(
            engine,
            "healthy",
            status=ProviderStatus.IDLE,
            next_run_at=NOW + timedelta(hours=1),
            retry_step=0,
            consecutive_failures=0,
            now=NOW,
            run_id=first_id,
            run_status=RunStatus.SUCCESS,
        )

    async with transaction(engine) as conn:
        statuses = [row.status for row in await conn.execute(select(sync_runs.c.status))]
        state = (await conn.execute(select(provider_state.c.next_run_at))).scalar_one()
    assert statuses == [str(RunStatus.RUNNING), str(RunStatus.RUNNING)]
    assert state == NOW.replace(tzinfo=None)


async def test_stale_run_completion_cannot_replace_recovered_state(
    engine: _SyncAsyncEngine, static_metadata: None
) -> None:
    await _insert_provider(engine, "healthy", next_run_at=NOW)
    assert await claim(engine, "healthy", now=NOW)
    async with transaction(engine) as conn:
        result = await conn.execute(
            sync_runs.insert()
            .values(
                provider_id="healthy",
                lineage_id=uuid.uuid4(),
                attempt=1,
                mode="incremental",
                status=str(RunStatus.RUNNING),
                started_at=NOW,
            )
            .returning(sync_runs.c.id)
        )
        run_id = int(result.scalar_one())
        recovered_schedule = NOW + timedelta(minutes=5)
        await conn.execute(
            update(sync_runs)
            .where(sync_runs.c.id == run_id)
            .values(status=str(RunStatus.FAILED), finished_at=NOW)
        )
        await conn.execute(
            update(providers)
            .where(providers.c.id == "healthy")
            .values(status=str(ProviderStatus.IDLE))
        )
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "healthy")
            .values(next_run_at=recovered_schedule)
        )

    await release(
        engine,
        "healthy",
        status=ProviderStatus.DEGRADED,
        next_run_at=NOW + timedelta(hours=1),
        retry_step=3,
        consecutive_failures=4,
        now=NOW + timedelta(seconds=1),
        run_id=run_id,
        run_status=RunStatus.SUCCESS,
    )

    provider, state = await _provider_and_state(engine, "healthy")
    assert provider.status == str(ProviderStatus.IDLE)
    assert state.next_run_at == recovered_schedule.replace(tzinfo=None)
    assert state.retry_step == 0


async def test_guarded_dispatch_persists_host_error_details(
    engine: _SyncAsyncEngine, static_metadata: None
) -> None:
    await _insert_provider(engine, "healthy")
    assert await claim(engine, "healthy", now=NOW)

    async def fail(_: DueProvider) -> None:
        raise RuntimeError("dispatch exploded")

    scheduler = Scheduler(engine, dispatch=fail, clock=FrozenClock(NOW))
    await scheduler._guarded(
        DueProvider(
            provider_id="healthy",
            interval_seconds=3600,
            retry_step=0,
            consecutive_failures=0,
            cursor=None,
        )
    )

    provider, state = await _provider_and_state(engine, "healthy")
    assert provider.status == str(ProviderStatus.DEGRADED)
    assert provider.last_error["error_class"] == str(ErrorClass.INTERNAL)
    assert "dispatch exploded" in provider.last_error["message"]
    assert state.next_run_at == (NOW + timedelta(hours=1)).replace(tzinfo=None)


async def test_two_actors_have_exactly_one_successful_claim(
    engine: _SyncAsyncEngine, static_metadata: None
) -> None:
    await _insert_provider(engine, "healthy")

    first, second = await asyncio.gather(
        claim(engine, "healthy", now=NOW),
        claim(engine, "healthy", now=NOW),
    )

    assert sorted((first, second)) == [False, True]
    provider, _ = await _provider_and_state(engine, "healthy")
    assert provider.status == str(ProviderStatus.SYNCING)


async def test_release_does_not_reenable_a_provider_disabled_while_running(
    engine: _SyncAsyncEngine, static_metadata: None
) -> None:
    lineage = uuid.uuid4()
    await _insert_provider(
        engine,
        "healthy",
        requested_mode="full",
        requested_lineage_id=lineage,
    )
    assert await claim(engine, "healthy", now=NOW)

    async with transaction(engine) as conn:
        await conn.execute(
            update(providers)
            .where(providers.c.id == "healthy")
            .values(enabled=False, status=str(ProviderStatus.DISABLED), updated_at=NOW)
        )

    next_run = NOW + timedelta(hours=1)
    await release(
        engine,
        "healthy",
        status=ProviderStatus.IDLE,
        next_run_at=next_run,
        retry_step=2,
        consecutive_failures=3,
        now=NOW,
    )

    provider, state = await _provider_and_state(engine, "healthy")
    assert provider.enabled is False
    assert provider.status == str(ProviderStatus.DISABLED)
    assert state.next_run_at == next_run.replace(tzinfo=None)
    assert state.retry_step == 2
    assert state.consecutive_failures == 3
    assert state.requested_mode == "full"
    assert state.requested_lineage_id == lineage


async def test_stop_cancels_and_awaits_remaining_dispatches(
    engine: _SyncAsyncEngine,
    static_metadata: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _insert_provider(engine, "healthy", next_run_at=NOW)
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def wait_for_stop(_: DueProvider) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    monkeypatch.setattr(scheduler_module, "SHUTDOWN_GRACE_SECONDS", 0)
    scheduler = Scheduler(engine, dispatch=wait_for_stop, clock=FrozenClock(NOW))

    assert await scheduler.poll_once() == 1
    await started.wait()
    await scheduler.stop()

    assert cleaned_up.is_set()
    assert await scheduler.poll_once() == 0


async def test_stop_during_claim_releases_provider_without_dispatch(
    engine: _SyncAsyncEngine,
    static_metadata: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _insert_provider(engine, "healthy")
    scheduler: Scheduler
    original_claim = scheduler_module.claim

    async def stop_after_claim(*args: object, **kwargs: object) -> bool:
        claimed = await original_claim(*args, **kwargs)  # type: ignore[arg-type]
        scheduler.request_stop()
        return claimed

    monkeypatch.setattr(scheduler_module, "claim", stop_after_claim)
    dispatched = False

    async def record_dispatch(_: DueProvider) -> None:
        nonlocal dispatched
        dispatched = True

    scheduler = Scheduler(engine, dispatch=record_dispatch, clock=FrozenClock(NOW))

    assert await scheduler.poll_once() == 0
    provider, _ = await _provider_and_state(engine, "healthy")
    assert provider.status == str(ProviderStatus.IDLE)
    assert not dispatched
