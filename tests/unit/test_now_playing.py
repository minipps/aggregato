"""Transient now-playing scheduling stays isolated from normal sync state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine, transaction
from aggregato.db.schema import image_cache, metadata, provider_state, providers
from aggregato.domain.enums import Capability, ErrorClass, MediaType, ProviderStatus, RunStatus
from aggregato.domain.models import NormalizedWork, NowPlayingItem
from aggregato.providers.registry import ProviderInfo
from aggregato.sync import now_playing as monitor_module
from aggregato.sync.now_playing import NOW_PLAYING_INTERVAL, NowPlayingMonitor, config_fingerprint
from aggregato.sync.runner import RunOutcome
from tests.conftest import FrozenClock

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'now-playing.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


def _info(provider_id: str = "fake") -> ProviderInfo:
    return ProviderInfo(
        id=provider_id,
        name="Fake",
        module="tests.providers.fake",
        media_types=frozenset({"track"}),
        capabilities=frozenset({Capability.NOW_PLAYING.value}),
        acquisition="api",
        schema_version=1,
        default_poll_interval=timedelta(hours=1),
        reviewed=True,
        api_visible=True,
        config_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string"}},
        },
        rating_scales=(),
    )


def _config(tmp_path: Path) -> Config:
    return load_config(
        {"AGGREGATO_TOKEN": "test", "AGGREGATO_DATA": str(tmp_path)},
        db_overrides={"providers": {"fake": {"name": "operator"}}},
    )


async def _seed(
    engine: AsyncEngine, *, provider_id: str = "fake", fingerprint: str | None = None
) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            providers.insert().values(
                id=provider_id,
                enabled=True,
                status=str(ProviderStatus.IDLE),
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
                effective_interval_seconds=3600,
                consecutive_failures=0,
                retry_step=0,
                now_playing_next_poll_at=NOW,
                now_playing_config_fingerprint=fingerprint,
                kv={},
            )
        )


def _item(title: str = "Track") -> NowPlayingItem:
    return NowPlayingItem(
        work=NormalizedWork(
            media_type=MediaType.TRACK,
            title=title,
            image_url="https://images.example/cover.jpg",
        )
    )


async def test_active_item_is_durable_and_equal_refresh_keeps_changed_at(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = _info()
    config = _config(tmp_path)
    fingerprint = config_fingerprint({"name": "operator"}, info.schema_version)
    await _seed(engine, fingerprint=fingerprint)
    monkeypatch.setattr(monitor_module, "discover_providers", lambda _dir: [info])

    async def fake_run(_request: object) -> RunOutcome:
        return _success(_item())

    monkeypatch.setattr(monitor_module, "execute_run", fake_run)
    clock = FrozenClock(NOW)
    monitor = NowPlayingMonitor(engine, config, clock=clock)

    assert await monitor.poll_once() == 1
    async with transaction(engine) as conn:
        first = (
            await conn.execute(
                select(
                    provider_state.c.now_playing_item,
                    provider_state.c.now_playing_changed_at,
                    provider_state.c.now_playing_checked_at,
                    provider_state.c.now_playing_next_poll_at,
                )
            )
        ).one()
        image = (await conn.execute(select(image_cache.c.source_url))).scalar_one()
    assert first.now_playing_item["work"]["title"] == "Track"
    assert _aware(first.now_playing_changed_at) == NOW
    assert _aware(first.now_playing_checked_at) == NOW
    assert _aware(first.now_playing_next_poll_at) == NOW + NOW_PLAYING_INTERVAL
    assert image == "https://images.example/cover.jpg"

    clock.advance(timedelta(seconds=15))
    await monitor.poll_once()
    async with transaction(engine) as conn:
        second = (await conn.execute(select(provider_state.c.now_playing_changed_at))).scalar_one()
    assert _aware(second) == NOW


async def test_transient_ladder_and_permanent_failure_suspend_until_reactivation(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = _info()
    config = _config(tmp_path)
    fingerprint = config_fingerprint({"name": "operator"}, info.schema_version)
    await _seed(engine, fingerprint=fingerprint)
    monkeypatch.setattr(monitor_module, "discover_providers", lambda _dir: [info])
    outcomes = iter(
        [
            RunOutcome(RunStatus.FAILED, error_class=ErrorClass.TRANSPORT),
            RunOutcome(RunStatus.FAILED, error_class=ErrorClass.AUTH),
        ]
    )

    async def fake_run(_request: object) -> RunOutcome:
        return next(outcomes)

    monkeypatch.setattr(monitor_module, "execute_run", fake_run)
    clock = FrozenClock(NOW)
    monitor = NowPlayingMonitor(engine, config, clock=clock)

    await monitor.poll_once()
    async with transaction(engine) as conn:
        first = (
            await conn.execute(
                select(
                    provider_state.c.now_playing_failures,
                    provider_state.c.now_playing_next_poll_at,
                )
            )
        ).one()
    assert first.now_playing_failures == 1
    assert _aware(first.now_playing_next_poll_at) == NOW + timedelta(minutes=1)

    clock.advance(timedelta(minutes=1))
    await monitor.poll_once()
    async with transaction(engine) as conn:
        second = (
            await conn.execute(
                select(
                    provider_state.c.now_playing_failures,
                    provider_state.c.now_playing_next_poll_at,
                )
            )
        ).one()
    assert second.now_playing_failures == 2
    assert second.now_playing_next_poll_at is None

    outcomes = iter([_success(None)])
    await monitor._reactivate("fake", fingerprint, clock.now())
    assert await monitor.poll_once() == 1


async def test_child_concurrency_is_capped_at_three(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info_ids = [f"fake-{index}" for index in range(5)]
    infos = [_info(provider_id) for provider_id in info_ids]
    config = _config(tmp_path)
    for provider_id in info_ids:
        await _seed(engine, provider_id=provider_id)
    monkeypatch.setattr(monitor_module, "discover_providers", lambda _dir: infos)
    active = 0
    peak = 0

    async def fake_run(_request: object) -> RunOutcome:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return _success(None)

    monkeypatch.setattr(monitor_module, "execute_run", fake_run)
    monitor = NowPlayingMonitor(engine, config, clock=FrozenClock(NOW))
    await monitor.poll_once()
    assert peak <= 3


def _success(item: NowPlayingItem | None) -> RunOutcome:
    return RunOutcome(
        RunStatus.SUCCESS,
        now_playing=item,
        now_playing_result_received=True,
    )


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo is not None else value.replace(tzinfo=UTC)
