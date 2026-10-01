"""The live sync websocket uses the same authenticated snapshot as HTTP."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from aggregato.config import load_config
from aggregato.db.engine import transaction
from aggregato.db.schema import sync_runs
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
    disconnect = asyncio.Event()
    connected = False

    async def receive() -> dict[str, object]:
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await disconnect.wait()
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
        disconnect.set()
        await asyncio.wait_for(task, timeout=2)

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
    connected = False

    async def receive() -> dict[str, object]:
        nonlocal connected
        if connected:
            raise AssertionError("rejected handshake should not read another websocket message")
        connected = True
        return {"type": "websocket.connect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    async with app.router.lifespan_context(app):
        await app(_scope(app), receive, send)

    close_codes = {
        message.get("code") for message in messages if message["type"] == "websocket.close"
    }
    assert close_codes == {1008}


async def test_readonly_sync_websocket_omits_private_run_details(tmp_path: Path) -> None:
    config = load_config(
        {
            "AGGREGATO_TOKEN": "operator-token",
            "AGGREGATO_READONLY_TOKEN": "readonly-token",
            "AGGREGATO_DATA": str(tmp_path / "data"),
        }
    )
    app = create_app(config)
    messages: list[dict[str, object]] = []
    first_payload = asyncio.Event()
    disconnect = asyncio.Event()
    connected = False

    async with app.router.lifespan_context(app):
        async with transaction(app.state.engine) as conn:
            await conn.execute(
                sync_runs.insert().values(
                    provider_id="fixture",
                    lineage_id=uuid.uuid4(),
                    attempt=1,
                    mode="incremental",
                    status="failed",
                    phase="failed",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    error_class="internal",
                    error_message="private-error-sentinel",
                    log_excerpt="private-log-sentinel",
                    cursor_before={"token": "private-cursor-sentinel"},
                    cursor_after={"token": "private-cursor-sentinel"},
                )
            )

        async def receive() -> dict[str, object]:
            nonlocal connected
            if not connected:
                connected = True
                return {"type": "websocket.connect"}
            await disconnect.wait()
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)
            if message["type"] == "websocket.send":
                first_payload.set()

        scope = _scope(
            app,
            headers=[(b"authorization", b"Bearer readonly-token"), (b"host", b"test")],
        )
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first_payload.wait(), timeout=2)
        disconnect.set()
        await asyncio.wait_for(task, timeout=2)

    payload = json.loads(
        next(message["text"] for message in messages if message["type"] == "websocket.send")
    )
    run = payload["runs"][0]
    assert run["error_class"] == "internal"
    assert run["error_message"] is None
    assert run["log_excerpt"] is None
    assert run["cursor_before"] is None
    assert run["cursor_after"] is None
    assert "private-error-sentinel" not in str(payload)
    assert "private-log-sentinel" not in str(payload)
    assert "private-cursor-sentinel" not in str(payload)
