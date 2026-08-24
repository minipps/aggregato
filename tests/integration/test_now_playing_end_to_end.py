"""A supervised now-playing child becomes durable API state across an API restart."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config, load_config
from aggregato.db.engine import transaction
from aggregato.db.schema import entries, provider_items, provider_state, providers, sync_runs
from aggregato.domain.enums import ProviderStatus
from aggregato.main import create_app
from aggregato.sync.now_playing import NowPlayingMonitor, config_fingerprint
from tests.conftest import FrozenClock

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
TOKEN = "now-playing-integration-token"


def _write_provider(
    provider_dir: Path,
    provider_id: str,
    fixture: Path,
    *,
    fails: bool = False,
) -> None:
    package = provider_dir / provider_id
    package.mkdir(parents=True)
    class_name = "RecordedNowPlayingProvider"
    source = f"""import json
from pathlib import Path

from aggregato.domain.enums import Capability, MediaType
from aggregato.domain.models import NowPlayingItem, NormalizedWork
from aggregato.providers.errors import TransportError
from aggregato.providers.fixture import FixtureProvider


class {class_name}(FixtureProvider):
    id = {provider_id!r}
    media_types = {{MediaType.TRACK}}
    capabilities = {{Capability.NOW_PLAYING}}

    async def now_playing(self, ctx):
        if {fails!r}:
            raise TransportError("recorded provider failed")
        body = json.loads(Path(ctx.config.path).read_text(encoding="utf-8"))
        if not body["active"]:
            return None
        return NowPlayingItem(
            work=NormalizedWork(media_type=MediaType.TRACK, title=body["title"])
        )


provider = {class_name}()
"""
    (package / "__init__.py").write_text(source, encoding="utf-8")
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "name": provider_id,
                "media_types": ["track"],
                "capabilities": ["now_playing"],
                "acquisition": "export",
                "schema_version": 1,
                "default_poll_interval_seconds": 3600,
                "config_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path"],
                    "properties": {"path": {"type": "string", "format": "path"}},
                },
            }
        ),
        encoding="utf-8",
    )


async def _seed_provider(
    app: FastAPI,
    provider_id: str,
    fixture: Path,
    now: datetime,
) -> None:
    settings = {"path": str(fixture)}
    async with transaction(app.state.engine) as conn:
        await conn.execute(
            providers.insert().values(
                id=provider_id,
                enabled=True,
                status=str(ProviderStatus.IDLE),
                acquisition="export",
                schema_version=1,
                reviewed=False,
                config=settings,
                created_at=now,
                updated_at=now,
            )
        )
        await conn.execute(
            provider_state.insert().values(
                provider_id=provider_id,
                effective_interval_seconds=3600,
                now_playing_next_poll_at=now,
                now_playing_config_fingerprint=config_fingerprint(settings, 1),
                kv={},
            )
        )


def _scope(app: FastAPI, token: str) -> dict[str, object]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/api/v1/ws/now-playing",
        "raw_path": b"/api/v1/ws/now-playing",
        "query_string": b"",
        "headers": [(b"host", b"test"), (b"authorization", f"Bearer {token}".encode())],
        "client": ("test", 0),
        "server": ("test", 80),
        "subprotocols": [],
        "root_path": "",
        "app": app,
    }


async def _snapshot(app: FastAPI) -> dict[str, object]:
    received = asyncio.Event()
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)
        if message["type"] == "websocket.send":
            received.set()

    task = asyncio.create_task(app(_scope(app, TOKEN), receive, send))
    await asyncio.wait_for(received.wait(), timeout=2)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return json.loads(
        next(message["text"] for message in messages if message["type"] == "websocket.send")
    )


async def _count(engine: AsyncEngine, table: object) -> int:
    async with transaction(engine) as conn:
        return int((await conn.execute(select(func.count()).select_from(table))).scalar_one())


async def _state(engine: AsyncEngine, provider_id: str) -> Mapping[str, object]:
    async with transaction(engine) as conn:
        return (
            (
                await conn.execute(
                    select(provider_state).where(provider_state.c.provider_id == provider_id)
                )
            )
            .mappings()
            .one()
        )


def _config(data_dir: Path, provider_dir: Path) -> Config:
    return load_config(
        {
            "AGGREGATO_TOKEN": TOKEN,
            "AGGREGATO_DATA": str(data_dir),
            "AGGREGATO_PROVIDER_DIR": str(provider_dir),
        }
    )


async def test_child_state_reaches_authenticated_ws_and_survives_restart(
    tmp_path: Path,
) -> None:
    provider_dir = tmp_path / "providers"
    active_fixture = tmp_path / "active.json"
    failing_fixture = tmp_path / "failing.json"
    active_fixture.write_text(json.dumps({"active": True, "title": "First track"}))
    failing_fixture.write_text(json.dumps({"active": True, "title": "Unused"}))
    _write_provider(provider_dir, "recorded_active", active_fixture)
    _write_provider(provider_dir, "recorded_failure", failing_fixture, fails=True)

    clock = FrozenClock(NOW)
    config = _config(tmp_path / "data", provider_dir)
    app = create_app(config, clock=clock)
    async with app.router.lifespan_context(app):
        await _seed_provider(app, "recorded_active", active_fixture, NOW)
        await _seed_provider(app, "recorded_failure", failing_fixture, NOW)

        monitor = NowPlayingMonitor(app.state.engine, config, clock=clock)
        assert await monitor.poll_once() == 2

        payload = await _snapshot(app)
        assert [item["provider_id"] for item in payload["items"]] == ["recorded_active"]
        assert payload["items"][0]["work"]["title"] == "First track"
        failed = await _state(app.state.engine, "recorded_failure")
        assert failed["now_playing_failures"] == 1
        assert _aware(failed["now_playing_next_poll_at"]) == NOW + timedelta(minutes=1)
        assert await _count(app.state.engine, entries) == 0
        assert await _count(app.state.engine, provider_items) == 0
        assert await _count(app.state.engine, sync_runs) == 0

        clock.advance(timedelta(seconds=46))
        assert (await _snapshot(app))["items"] == []

        active_fixture.write_text(json.dumps({"active": True, "title": "Second track"}))
        clock.advance(timedelta(seconds=15))
        assert await monitor.poll_once() == 2
        payload = await _snapshot(app)
        assert payload["items"][0]["work"]["title"] == "Second track"

    restarted = create_app(config, clock=clock)
    async with restarted.router.lifespan_context(restarted):
        payload = await _snapshot(restarted)
        assert payload["items"][0]["work"]["title"] == "Second track"

        active_fixture.write_text(json.dumps({"active": False, "title": "Second track"}))
        clock.advance(timedelta(seconds=15))
        monitor = NowPlayingMonitor(restarted.state.engine, config, clock=clock)
        assert await monitor.poll_once() == 1
        assert (await _snapshot(restarted))["items"] == []


def _aware(value: object) -> object:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
