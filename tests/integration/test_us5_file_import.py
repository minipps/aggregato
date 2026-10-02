"""Phase 7: an export upload reaches the normal worker-owned ingest path."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
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
EXPORT = Path("tests/fixtures/fixture/log-two-pages.jsonl").resolve()
UPLOAD_NAME = "fixture.xml"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config: Config = load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)})
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as session,
    ):
        yield session


@pytest.fixture
async def quota_client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config: Config = load_config(
        {
            "AGGREGATO_TOKEN": TOKEN,
            "AGGREGATO_DATA": str(data_dir),
            "AGGREGATO_IMPORT_QUOTA_BYTES": "5",
            "AGGREGATO_IMPORT_TOTAL_QUOTA_BYTES": "10",
        }
    )
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as session,
    ):
        yield session


@pytest.fixture
async def total_quota_client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config: Config = load_config(
        {
            "AGGREGATO_TOKEN": TOKEN,
            "AGGREGATO_DATA": str(data_dir),
            "AGGREGATO_IMPORT_QUOTA_BYTES": "10",
            "AGGREGATO_IMPORT_TOTAL_QUOTA_BYTES": "5",
        }
    )
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as session,
    ):
        yield session


def _engine(client: httpx2.AsyncClient) -> object:
    transport = client._transport_for_url(httpx2.URL("http://test/"))
    assert isinstance(transport, httpx2.ASGITransport)
    return transport.app.state.engine  # type: ignore[union-attr]


def _import_files(client: httpx2.AsyncClient, provider_id: str) -> list[Path]:
    transport = client._transport_for_url(httpx2.URL("http://test/"))
    assert isinstance(transport, httpx2.ASGITransport)
    data_dir = transport.app.state.config.data_dir  # type: ignore[union-attr]
    directory = data_dir / "imports" / provider_id
    return sorted(path for path in directory.iterdir() if path.is_file())


def _all_import_files(client: httpx2.AsyncClient) -> list[Path]:
    transport = client._transport_for_url(httpx2.URL("http://test/"))
    assert isinstance(transport, httpx2.ASGITransport)
    data_dir = transport.app.state.config.data_dir  # type: ignore[union-attr]
    directory = data_dir / "imports"
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and not path.name.endswith(".uploading")
    )


async def _enable_fixture(client: httpx2.AsyncClient) -> None:
    configured = await client.put(
        "/api/v1/providers/fixture/config",
        json={"path": str(EXPORT)},
    )
    assert configured.status_code == 200
    enabled = await client.post("/api/v1/providers/fixture/enable")
    assert enabled.status_code == 200


async def _count_items(engine: object) -> int:
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        result = await conn.execute(select(func.count()).select_from(provider_items))
        return int(result.scalar_one())


async def _run_queued_import(client: httpx2.AsyncClient) -> None:
    engine = _engine(client)
    now = datetime.now(UTC)
    due = await due_providers(engine, now=now)  # type: ignore[arg-type]
    assert len(due) == 1
    assert await claim(engine, "fixture", now=now)  # type: ignore[arg-type]
    transport = client._transport_for_url(httpx2.URL("http://test/"))
    assert isinstance(transport, httpx2.ASGITransport)
    await build_dispatch(engine, transport.app.state.config)(due[0])  # type: ignore[arg-type]
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        job = (
            await conn.execute(
                select(import_jobs.c.started_at, import_jobs.c.finished_at, import_jobs.c.failed_at)
                .where(import_jobs.c.provider_id == "fixture")
                .order_by(import_jobs.c.id.desc())
                .limit(1)
            )
        ).first()
    assert job is not None
    assert job.started_at is not None
    assert job.finished_at is not None
    assert job.failed_at is None


async def test_upload_imports_and_reupload_is_idempotent(client: httpx2.AsyncClient) -> None:
    await _enable_fixture(client)
    content = await asyncio.to_thread(EXPORT.read_bytes)
    response = await client.post(
        "/api/v1/providers/fixture/import",
        files={"file": (UPLOAD_NAME, content, "application/xml")},
    )
    assert response.status_code == 202
    await _run_queued_import(client)
    assert await _count_items(_engine(client)) == 5
    assert _all_import_files(client) == []

    response = await client.post(
        "/api/v1/providers/fixture/import",
        files={"file": (UPLOAD_NAME, content, "application/xml")},
    )
    assert response.status_code == 202
    await _run_queued_import(client)
    assert await _count_items(_engine(client)) == 5
    assert _all_import_files(client) == []


async def test_import_quota_includes_upload_and_serializes_concurrent_admission(
    quota_client: httpx2.AsyncClient,
) -> None:
    await _enable_fixture(quota_client)
    responses = await asyncio.gather(
        *(
            quota_client.post(
                "/api/v1/providers/fixture/import",
                files={"file": (UPLOAD_NAME, b"12345", "application/xml")},
            )
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [202, 413]
    async with transaction(_engine(quota_client)) as conn:  # type: ignore[arg-type]
        result = await conn.execute(select(func.count()).select_from(import_jobs))
        assert result.scalar_one() == 1
    files = _import_files(quota_client, "fixture")
    assert len(files) == 1
    assert files[0].suffix == ".xml"
    assert files[0].stat().st_size == 5


async def test_total_import_quota_serializes_admission_across_fixture_uploads(
    total_quota_client: httpx2.AsyncClient,
) -> None:
    await _enable_fixture(total_quota_client)

    responses = await asyncio.gather(
        total_quota_client.post(
            "/api/v1/providers/fixture/import",
            files={"file": (UPLOAD_NAME, b"12345", "application/xml")},
        ),
        total_quota_client.post(
            "/api/v1/providers/fixture/import",
            files={"file": (UPLOAD_NAME, b"12345", "application/xml")},
        ),
    )

    assert sorted(response.status_code for response in responses) == [202, 413]
    async with transaction(_engine(total_quota_client)) as conn:  # type: ignore[arg-type]
        result = await conn.execute(select(func.count()).select_from(import_jobs))
        assert result.scalar_one() == 1
    files = _all_import_files(total_quota_client)
    assert len(files) == 1
    assert files[0].stat().st_size == 5


async def test_unrelated_upload_is_rejected_without_a_job(client: httpx2.AsyncClient) -> None:
    await _enable_fixture(client)
    response = await client.post(
        "/api/v1/providers/fixture/import",
        files={"file": ("not-an-export.txt", b"not an export", "text/plain")},
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    async with transaction(_engine(client)) as conn:  # type: ignore[arg-type]
        result = await conn.execute(select(func.count()).select_from(import_jobs))
        assert result.scalar_one() == 0
    assert await _count_items(_engine(client)) == 0
