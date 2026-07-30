"""US7: a secret-free archive restores into a fresh browsable instance without a sync."""

from __future__ import annotations

import asyncio
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from aggregato.config import Config, load_config
from aggregato.export import restore_archive
from aggregato.main import create_app
from aggregato.sync.dispatch import build_dispatch
from aggregato.sync.scheduler import claim, due_providers

TOKEN = "phase-9-export-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
EXPORT = Path("tests/fixtures/goodreads/library_export.csv")


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    data = tmp_path / "source"
    data.mkdir()
    app = create_app(load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data)}))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as session,
    ):
        yield session


def _app(client: httpx.AsyncClient):
    transport = client._transport_for_url(httpx.URL("http://test/"))
    assert isinstance(transport, httpx.ASGITransport)
    return transport.app


async def _import_fixture(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/v1/providers/goodreads/enable")).status_code == 200
    response = await client.post(
        "/api/v1/providers/goodreads/import",
        files={
            "file": ("library_export.csv", await asyncio.to_thread(EXPORT.read_bytes), "text/csv")
        },
    )
    assert response.status_code == 202
    app = _app(client)
    due = await due_providers(app.state.engine, now=datetime.now(UTC))
    assert await claim(app.state.engine, "goodreads", now=datetime.now(UTC))
    await build_dispatch(app.state.engine, app.state.config)(due[0])


async def test_export_restores_browsable_archive_without_syncs(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    await _import_fixture(client)
    exported = await client.get("/api/v1/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/octet-stream")
    archive = tmp_path / "archive.zip"
    archive.write_bytes(exported.content)
    with zipfile.ZipFile(archive) as contents:
        config = contents.read("configuration.json")
        assert TOKEN.encode() not in config
        assert b"token" not in config

    restored_data = tmp_path / "restored"
    restored_config = restore_archive(archive, restored_data)
    assert "api" in restored_config
    restored: Config = load_config(
        {"AGGREGATO_TOKEN": "fresh-token", "AGGREGATO_DATA": str(restored_data)}
    )
    app = create_app(restored)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://restored",
            headers={"Authorization": "Bearer fresh-token"},
        ) as fresh,
    ):
        entries = await fresh.get("/api/v1/entries")
        assert entries.status_code == 200
        assert len(entries.json()["items"]) == 1


async def test_settings_report_storage_and_disable_image_cache(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/settings")
    assert response.status_code == 200
    assert (
        response.json()["failure_run_retention_days"]
        > response.json()["success_run_retention_days"]
    )
    changed = await client.patch("/api/v1/settings", json={"image_cache_enabled": False})
    assert changed.status_code == 200
    assert changed.json()["image_cache_enabled"] is False
    image = await client.get("/api/v1/media/image/not-a-source")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/gif"
