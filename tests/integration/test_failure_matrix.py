"""The operator-visible failure matrix from quickstart.md ."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine
from aggregato.db.schema import metadata, provider_state, providers, sync_runs
from aggregato.db.search import create_search_index
from aggregato.domain.enums import ErrorClass, FetchMode, ProviderStatus, RunStatus
from aggregato.domain.models import Cursor
from aggregato.sync.dispatch import run_once
from aggregato.sync.runner import RunOutcome
from tests.conftest import FrozenClock

NOW = datetime(2026, 3, 1, tzinfo=UTC)
FIXTURE = (Path(__file__).parent.parent / "fixtures/fixture/log-two-pages.jsonl").resolve()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'matrix.db'}")
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


def config() -> Config:
    return load_config(
        {"AGGREGATO_TOKEN": "matrix", "AGGREGATO_DATA": "./data"},
        db_overrides={"providers": {"fixture": {"path": str(FIXTURE)}}},
    )


@pytest.mark.parametrize(
    ("case", "outcome", "status", "retry", "action"),
    [
        (
            "transport / 5xx",
            RunOutcome(RunStatus.FAILED, error_class=ErrorClass.TRANSPORT, error_message="503"),
            ProviderStatus.IDLE,
            True,
            "automatically",
        ),
        (
            "rate_limit with Retry-After",
            RunOutcome(RunStatus.FAILED, error_class=ErrorClass.RATE_LIMIT, error_message="429"),
            ProviderStatus.IDLE,
            True,
            "slow down",
        ),
        (
            "auth",
            RunOutcome(RunStatus.FAILED, error_class=ErrorClass.AUTH, error_message="bad token"),
            ProviderStatus.DEGRADED,
            False,
            "credentials",
        ),
        (
            "blocked",
            RunOutcome(RunStatus.FAILED, error_class=ErrorClass.BLOCKED, error_message="captcha"),
            ProviderStatus.DEGRADED,
            False,
            "block",
        ),
        (
            "structure_changed",
            RunOutcome(
                RunStatus.FAILED, error_class=ErrorClass.STRUCTURE_CHANGED, error_message="shape"
            ),
            ProviderStatus.DEGRADED,
            False,
            "updating",
        ),
        (
            "mid-run failure after 2 pages",
            RunOutcome(
                RunStatus.PARTIAL,
                cursor_after=Cursor(state={"next_page": 3}),
                error_class=ErrorClass.TRANSPORT,
                error_message="lost",
            ),
            ProviderStatus.IDLE,
            True,
            "automatically",
        ),
        (
            "one permanently-failing record",
            RunOutcome(RunStatus.SUCCESS),
            ProviderStatus.IDLE,
            True,
            "",
        ),
        (
            "provider child killed (SIGKILL)",
            RunOutcome(RunStatus.FAILED, error_class=ErrorClass.INTERNAL, error_message="killed"),
            ProviderStatus.IDLE,
            True,
            "logs",
        ),
        (
            "provider child hangs",
            RunOutcome(
                RunStatus.FAILED, error_class=ErrorClass.INTERNAL, error_message="wall-clock"
            ),
            ProviderStatus.IDLE,
            True,
            "logs",
        ),
        (
            "provider config invalid",
            RunOutcome(
                RunStatus.FAILED, error_class=ErrorClass.INTERNAL, error_message="invalid config"
            ),
            ProviderStatus.IDLE,
            True,
            "logs",
        ),
    ],
)
async def test_failure_matrix_records_the_outcome_and_an_operator_action(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    outcome: RunOutcome,
    status: ProviderStatus,
    retry: bool,
    action: str,
) -> None:
    """One parameter row per quickstart failure; inject child behaviour at its boundary."""

    async def fake_execute(_: object) -> RunOutcome:
        return outcome

    monkeypatch.setattr("aggregato.sync.dispatch.execute_run", fake_execute)
    clock = FrozenClock(NOW)
    await run_once(
        engine,
        config(),
        provider_id="fixture",
        mode=FetchMode.INCREMENTAL,
        cursor=None,
        retry_step=0,
        consecutive_failures=0,
        interval_seconds=3600,
        clock=clock,
        lineage_id=uuid.uuid4(),
    )
    async with engine.connect() as conn:
        run = (await conn.execute(select(sync_runs))).one()
        provider = (await conn.execute(select(providers.c.status, providers.c.last_error))).one()
        next_run = (await conn.execute(select(provider_state.c.next_run_at))).scalar_one()
    assert run.status == str(outcome.status), case
    assert provider.status == str(status), case
    assert (next_run is not None) is retry, case
    if action:
        assert action in provider.last_error["action_required"].casefold(), case
