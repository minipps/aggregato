"""Schema replay preserves prior facts when one retained payload cannot be replaced."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

import aggregato.sync.dispatch as dispatch_module
from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine, transaction
from aggregato.db.schema import (
    entries,
    metadata,
    opinions,
    provider_items,
    provider_state,
    providers,
    sync_runs,
)
from aggregato.db.search import SearchKind, create_search_index, matching_ref_ids
from aggregato.domain.enums import (
    EntryKind,
    FetchMode,
    LoggedPrecision,
    MediaType,
    ProviderStatus,
    ReviewFormat,
    RunStatus,
)
from aggregato.domain.models import (
    NormalizedBatch,
    NormalizedEntry,
    NormalizedOpinion,
    NormalizedWork,
    RawRecord,
)
from aggregato.providers.manifest import BUNDLED_MANIFESTS
from aggregato.sync.dispatch import run_once
from aggregato.sync.protocol import FailureMessage
from aggregato.sync.runner import RunOutcome, RunRequest

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
FIXTURE = (Path(__file__).parent.parent / "fixtures/fixture/log-two-pages.jsonl").resolve()


def _fts5_available() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()
    return True


pytestmark = pytest.mark.skipif(not _fts5_available(), reason="the writer maintains an FTS5 index")


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}")
    async with engine.begin() as conn:
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
        yield engine
    finally:
        await engine.dispose()


def config_for(*, infer_deletes: bool = False) -> Config:
    settings: dict[str, object] = {"path": str(FIXTURE)}
    if infer_deletes:
        settings["infer_deletes"] = True
    return load_config(
        {"AGGREGATO_TOKEN": "replay-test", "AGGREGATO_DATA": "./data"},
        db_overrides={"providers": {"fixture": settings}},
    )


def record(native_id: str) -> RawRecord:
    return RawRecord(native_id=native_id, payload={"id": native_id})


def batch(native_id: str, *, review: str = "distinctive old review") -> NormalizedBatch:
    return NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title=f"Title {native_id}"),
        entries=[
            NormalizedEntry(
                kind=EntryKind.WATCH,
                logged_at=NOW,
                logged_precision=LoggedPrecision.EXACT,
                native_id=f"event-{native_id}",
            )
        ],
        opinions=[NormalizedOpinion(review_text=review, review_format=ReviewFormat.PLAIN)],
    )


async def run(
    engine: AsyncEngine,
    config: Config,
    clock: FixedClock,
    *,
    mode: FetchMode = FetchMode.INCREMENTAL,
) -> RunOutcome:
    return await run_once(
        engine,
        config,
        provider_id="fixture",
        mode=mode,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
    )


async def active_fact_counts(engine: AsyncEngine) -> tuple[int, int]:
    async with transaction(engine) as conn:
        active_entries = await conn.execute(
            select(entries.c.id).where(entries.c.deleted_at.is_(None))
        )
        active_opinions = await conn.execute(
            select(opinions.c.id).where(opinions.c.deleted_at.is_(None))
        )
        return len(active_entries.all()), len(active_opinions.all())


@pytest.mark.parametrize("rejection", ["normalize", "writer"])
async def test_replay_rejection_preserves_old_facts_and_version(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    rejection: str,
) -> None:
    clock = FixedClock()
    old_record = record("item-1")

    async def execute(request: RunRequest, **_kwargs: Any) -> RunOutcome:
        if not request.replay_records:
            return RunOutcome(status=RunStatus.SUCCESS, records=[(old_record, batch("item-1"))])
        replayed = request.replay_records[0]
        if rejection == "normalize":
            return RunOutcome(
                status=RunStatus.SUCCESS,
                failures=[
                    FailureMessage(
                        native_id=replayed.native_id,
                        payload=replayed.payload,
                        error="injected normalization rejection",
                    )
                ],
            )
        invalid = NormalizedBatch(
            work=NormalizedWork(media_type=MediaType.FILM, title="Replacement"),
            opinions=[NormalizedOpinion(rating_raw=4, rating_scale_id="undeclared-scale")],
        )
        return RunOutcome(status=RunStatus.SUCCESS, records=[(replayed, invalid)])

    monkeypatch.setattr(dispatch_module, "execute_run", execute)
    assert (await run(engine, config_for(), clock)).status is RunStatus.SUCCESS
    monkeypatch.setitem(BUNDLED_MANIFESTS["fixture"], "schema_version", 2)
    clock.advance(timedelta(minutes=1))

    outcome = await run(engine, config_for(), clock)

    assert outcome.status is RunStatus.PARTIAL
    assert await active_fact_counts(engine) == (1, 1)
    async with transaction(engine) as conn:
        version = (await conn.execute(select(provider_items.c.schema_version))).scalar_one()
        old_reviews = await matching_ref_ids(conn, SearchKind.REVIEW_TEXT, "distinctive")
    assert version == 1
    assert len(old_reviews) == 1


async def test_failed_full_run_never_infers_deletes(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FixedClock()
    old_record = record("item-1")
    runs = 0

    async def execute(_request: RunRequest, **_kwargs: Any) -> RunOutcome:
        nonlocal runs
        runs += 1
        if runs == 1:
            return RunOutcome(status=RunStatus.SUCCESS, records=[(old_record, batch("item-1"))])
        return RunOutcome(
            status=RunStatus.SUCCESS,
            failures=[
                FailureMessage(
                    native_id="missing-record",
                    payload={"id": "missing-record"},
                    error="injected normalization rejection",
                )
            ],
        )

    monkeypatch.setattr(dispatch_module, "execute_run", execute)
    config = config_for(infer_deletes=True)
    assert (await run(engine, config, clock, mode=FetchMode.FULL)).status is RunStatus.SUCCESS
    clock.advance(timedelta(minutes=1))

    outcome = await run(engine, config, clock, mode=FetchMode.FULL)

    assert outcome.status is RunStatus.SUCCESS
    async with transaction(engine) as conn:
        deleted_at = (await conn.execute(select(entries.c.deleted_at))).scalar_one()
    assert deleted_at is None


async def test_full_run_guard_checks_failures_even_without_a_failed_count(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FixedClock()
    old_record = record("item-1")

    async def execute(_request: RunRequest, **_kwargs: Any) -> RunOutcome:
        return RunOutcome(status=RunStatus.SUCCESS, records=[(old_record, batch("item-1"))])

    monkeypatch.setattr(dispatch_module, "execute_run", execute)
    config = config_for(infer_deletes=True)
    assert (await run(engine, config, clock, mode=FetchMode.FULL)).status is RunStatus.SUCCESS
    clock.advance(timedelta(minutes=1))

    outcome = RunOutcome(
        status=RunStatus.SUCCESS,
        failures=[
            FailureMessage(
                native_id="missing-record",
                payload={"id": "missing-record"},
                error="injected normalization rejection",
            )
        ],
    )
    await dispatch_module._apply_full_run_guards(
        engine,
        provider=dispatch_module._provider_info("fixture", None),
        provider_id="fixture",
        mode=FetchMode.FULL,
        outcome=outcome,
        item_count=1,
        failed_count=0,
        run_started_at=clock.now(),
        config={"infer_deletes": True},
    )

    async with transaction(engine) as conn:
        deleted_at = (await conn.execute(select(entries.c.deleted_at))).scalar_one()
    assert deleted_at is None


async def test_interrupted_multibatch_replay_resumes_from_committed_items(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock()
    old_records = [record("item-a"), record("item-b"), record("item-c")]
    replayed_ids: list[str] = []

    async def seed(request: RunRequest, **_kwargs: Any) -> RunOutcome:
        return RunOutcome(
            status=RunStatus.SUCCESS,
            records=[(raw, batch(raw.native_id)) for raw in old_records],
        )

    monkeypatch.setattr(dispatch_module, "execute_run", seed)
    assert (await run(engine, config_for(), clock)).status is RunStatus.SUCCESS
    monkeypatch.setitem(BUNDLED_MANIFESTS["fixture"], "schema_version", 2)
    monkeypatch.setattr(dispatch_module, "_REPLAY_BATCH_RECORDS", 1)

    async def interrupt_after_first(request: RunRequest, **_kwargs: Any) -> RunOutcome:
        if not request.replay_records:
            return RunOutcome(status=RunStatus.SUCCESS)
        raw = request.replay_records[0]
        replayed_ids.append(raw.native_id)
        if len(replayed_ids) == 2:
            raise asyncio.CancelledError
        return RunOutcome(
            status=RunStatus.SUCCESS,
            records=[(raw, batch(raw.native_id, review="updated replay review"))],
        )

    monkeypatch.setattr(dispatch_module, "execute_run", interrupt_after_first)
    clock.advance(timedelta(minutes=1))
    with pytest.raises(asyncio.CancelledError):
        await run(engine, config_for(), clock)

    async with transaction(engine) as conn:
        versions = {
            row.native_id: row.schema_version
            for row in await conn.execute(
                select(provider_items.c.native_id, provider_items.c.schema_version)
            )
        }
        interrupted = (
            await conn.execute(select(sync_runs.c.status).order_by(sync_runs.c.id.desc()).limit(1))
        ).scalar_one()
    assert replayed_ids == ["item-a", "item-b"]
    assert versions == {"item-a": 2, "item-b": 1, "item-c": 1}
    assert interrupted == str(RunStatus.PARTIAL)

    replayed_ids.clear()

    async def resume(request: RunRequest, **_kwargs: Any) -> RunOutcome:
        if not request.replay_records:
            return RunOutcome(status=RunStatus.SUCCESS)
        raw = request.replay_records[0]
        replayed_ids.append(raw.native_id)
        return RunOutcome(
            status=RunStatus.SUCCESS,
            records=[(raw, batch(raw.native_id, review="updated replay review"))],
        )

    monkeypatch.setattr(dispatch_module, "execute_run", resume)
    clock.advance(timedelta(minutes=1))
    outcome = await run(engine, config_for(), clock)

    assert outcome.status is RunStatus.SUCCESS
    assert replayed_ids == ["item-b", "item-c"]
    async with transaction(engine) as conn:
        versions = {
            row.native_id: row.schema_version
            for row in await conn.execute(
                select(provider_items.c.native_id, provider_items.c.schema_version)
            )
        }
    assert versions == {"item-a": 2, "item-b": 2, "item-c": 2}
