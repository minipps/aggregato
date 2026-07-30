"""Phase 7: an export upload reaches the normal worker-owned ingest path."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from aggregato.config import Config, load_config
from aggregato.db.engine import transaction
from aggregato.db.schema import import_jobs, provider_items
from aggregato.main import create_app
from aggregato.sync.dispatch import build_dispatch
from aggregato.sync.scheduler import claim, due_providers

TOKEN = "phase-7-import-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
EXPORT = Path("tests/fixtures/goodreads/library_export.csv")


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config: Config = load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)})
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as session,
    ):
        yield session


def _engine(client: httpx.AsyncClient) -> object:
    transport = client._transport_for_url(httpx.URL("http://test/"))
    assert isinstance(transport, httpx.ASGITransport)
    return transport.app.state.engine  # type: ignore[union-attr]


async def _count_items(engine: object) -> int:
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        result = await conn.execute(select(func.count()).select_from(provider_items))
        return int(result.scalar_one())


async def _run_queued_import(client: httpx.AsyncClient) -> None:
    engine = _engine(client)
    now = datetime.now(UTC)
    due = await due_providers(engine, now=now)  # type: ignore[arg-type]
    assert len(due) == 1
    assert await claim(engine, "goodreads", now=now)  # type: ignore[arg-type]
    transport = client._transport_for_url(httpx.URL("http://test/"))
    assert isinstance(transport, httpx.ASGITransport)
    await build_dispatch(engine, transport.app.state.config)(due[0])  # type: ignore[arg-type]


async def test_upload_imports_and_reupload_is_idempotent(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/v1/providers/goodreads/enable")).status_code == 200
    content = await asyncio.to_thread(EXPORT.read_bytes)
    response = await client.post(
        "/api/v1/providers/goodreads/import",
        files={"file": ("library_export.csv", content, "text/csv")},
    )
    assert response.status_code == 202
    await _run_queued_import(client)
    assert await _count_items(_engine(client)) == 2

    response = await client.post(
        "/api/v1/providers/goodreads/import",
        files={"file": ("library_export.csv", content, "text/csv")},
    )
    assert response.status_code == 202
    await _run_queued_import(client)
    assert await _count_items(_engine(client)) == 2


async def test_unrelated_upload_is_rejected_without_a_job(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/v1/providers/goodreads/enable")).status_code == 200
    response = await client.post(
        "/api/v1/providers/goodreads/import",
        files={"file": ("not-an-export.txt", b"not an export", "text/plain")},
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    async with transaction(_engine(client)) as conn:  # type: ignore[arg-type]
        result = await conn.execute(select(func.count()).select_from(import_jobs))
        assert result.scalar_one() == 0
    assert await _count_items(_engine(client)) == 0
