"""The live sync websocket uses the same authenticated snapshot as HTTP."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

from aggregato.config import load_config
from aggregato.main import create_app


def _scope(app: object, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, object]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/api/v1/ws/sync",
        "raw_path": b"/api/v1/ws/sync",
        "query_string": b"",
        "headers": headers or [(b"host", b"test")],
        "client": ("test", 0),
        "server": ("test", 80),
        "subprotocols": [],
        "root_path": "",
        "app": app,
    }


async def test_sync_websocket_accepts_a_bearer_and_sends_a_snapshot(tmp_path: Path) -> None:
    token = "sync-websocket-token"
    config = load_config({"AGGREGATO_TOKEN": token, "AGGREGATO_DATA": str(tmp_path / "data")})
    app = create_app(config)

    messages: list[dict[str, object]] = []
    first_payload = asyncio.Event()
    connected = False

    async def receive() -> dict[str, object]:
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)
        if message["type"] == "websocket.send":
            first_payload.set()

    scope = _scope(
        app,
        headers=[(b"authorization", f"Bearer {token}".encode()), (b"host", b"test")],
    )

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first_payload.wait(), timeout=2)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    payload = json.loads(
        next(message["text"] for message in messages if message["type"] == "websocket.send")
    )

    assert payload["type"] == "snapshot"
    assert payload["providers"] == []
    assert payload["runs"] == []


async def test_sync_websocket_closes_unauthenticated_connections(tmp_path: Path) -> None:
    config = load_config(
        {"AGGREGATO_TOKEN": "sync-websocket-token", "AGGREGATO_DATA": str(tmp_path / "data")}
    )
    app = create_app(config)
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    async with app.router.lifespan_context(app):
        await app(_scope(app), receive, send)

    close_codes = {
        message.get("code") for message in messages if message["type"] == "websocket.close"
    }
    assert close_codes == {1008}
