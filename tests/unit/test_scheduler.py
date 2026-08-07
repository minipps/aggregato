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
from aggregato.db.schema import metadata, provider_state, providers
from aggregato.domain.enums import Capability, ProviderStatus
from aggregato.providers.registry import ProviderInfo
from aggregato.sync import scheduler as scheduler_module
from aggregato.sync.scheduler import claim, due_providers, release
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
        import_inference=None,
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
    next_run_at: datetime | None = None,
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
