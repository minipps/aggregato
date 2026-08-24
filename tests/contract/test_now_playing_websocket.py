"""The now-playing websocket is authenticated and serves durable, fresh snapshots."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI

from aggregato.api.schemas import image_path
from aggregato.config import load_config
from aggregato.db.engine import transaction
from aggregato.db.schema import provider_state, providers
from aggregato.domain.enums import MediaType
from aggregato.domain.models import NormalizedWork, NowPlayingItem
from aggregato.main import create_app


def _scope(
    app: FastAPI,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, object]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/api/v1/ws/now-playing",
        "raw_path": b"/api/v1/ws/now-playing",
        "query_string": b"",
        "headers": headers or [(b"host", b"test")],
        "client": ("test", 0),
        "server": ("test", 80),
        "subprotocols": [],
        "root_path": "",
        "app": app,
    }


def _item(title: str, image_url: str | None = None) -> dict[str, object]:
    return NowPlayingItem(
        work=NormalizedWork(media_type=MediaType.TRACK, title=title, image_url=image_url)
    ).model_dump(mode="json")


async def _seed(
    app: FastAPI,
    provider_id: str,
    *,
    item: dict[str, object] | None,
    changed_at,
    checked_at,
    enabled: bool = True,
) -> None:
    async with transaction(app.state.engine) as conn:
        await conn.execute(
            providers.insert().values(
                id=provider_id,
                enabled=enabled,
                status="idle",
                acquisition="api",
                schema_version=1,
                reviewed=True,
                config={},
                created_at=changed_at,
                updated_at=changed_at,
            )
        )
        await conn.execute(
            provider_state.insert().values(
                provider_id=provider_id,
                effective_interval_seconds=300,
                kv={},
                now_playing_item=item,
                now_playing_changed_at=changed_at,
                now_playing_checked_at=checked_at,
            )
        )


async def _first_snapshot(
    app: FastAPI, token: str
) -> tuple[dict[str, object], list[dict[str, object]]]:
    messages: list[dict[str, object]] = []
    first_payload = asyncio.Event()
    scope = _scope(app, headers=[(b"authorization", f"Bearer {token}".encode())])

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)
        if message["type"] == "websocket.send":
            first_payload.set()

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(first_payload.wait(), timeout=2)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    payload = json.loads(
        next(message["text"] for message in messages if message["type"] == "websocket.send")
    )
    return payload, messages


async def test_now_playing_sends_sorted_fresh_items_and_local_images(tmp_path: Path) -> None:
    token = "now-playing-token"
    config = load_config({"AGGREGATO_TOKEN": token, "AGGREGATO_DATA": str(tmp_path / "data")})
    app = create_app(config)
    now = app.state.clock.now()
    async with app.router.lifespan_context(app):
        await _seed(
            app,
            "listenbrainz",
            item=_item("zulu", "https://example.invalid/zulu.jpg"),
            changed_at=now - timedelta(seconds=1),
            checked_at=now - timedelta(seconds=1),
        )
        await _seed(
            app,
            "koito",
            item=_item("alpha"),
            changed_at=now - timedelta(seconds=1),
            checked_at=now - timedelta(seconds=1),
        )
        payload, _ = await _first_snapshot(app, token)

    assert payload["type"] == "snapshot"
    items = payload["items"]
    assert [item["provider_id"] for item in items] == ["koito", "listenbrainz"]
    assert items[1]["work"]["image"] == image_path("https://example.invalid/zulu.jpg")


async def test_now_playing_omits_disabled_stale_and_invalid_rows(tmp_path: Path) -> None:
    token = "now-playing-token"
    config = load_config({"AGGREGATO_TOKEN": token, "AGGREGATO_DATA": str(tmp_path / "data")})
    app = create_app(config)
    now = app.state.clock.now()
    async with app.router.lifespan_context(app):
        await _seed(
            app,
            "koito",
            item=_item("fresh"),
            changed_at=now - timedelta(seconds=1),
            checked_at=now - timedelta(seconds=1),
        )
        await _seed(
            app,
            "listenbrainz",
            item=_item("stale"),
            changed_at=now - timedelta(seconds=46),
            checked_at=now - timedelta(seconds=46),
        )
        await _seed(
            app,
            "anilist",
            item={"not": "a now-playing item"},
            changed_at=now - timedelta(seconds=1),
            checked_at=now - timedelta(seconds=1),
        )
        payload, _ = await _first_snapshot(app, token)

    assert [item["provider_id"] for item in payload["items"]] == ["koito"]


async def test_now_playing_rejects_unauthenticated_handshake(tmp_path: Path) -> None:
    config = load_config(
        {"AGGREGATO_TOKEN": "now-playing-token", "AGGREGATO_DATA": str(tmp_path / "data")}
    )
    app = create_app(config)
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    async with app.router.lifespan_context(app):
        await app(_scope(app), receive, send)

    assert {
        message.get("code") for message in messages if message["type"] == "websocket.close"
    } == {1008}
