""": a secret-free archive restores into a fresh browsable instance without a sync."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import make_url

from aggregato import export as export_module
from aggregato.config import Config, load_config
from aggregato.db.engine import transaction
from aggregato.db.schema import (
    import_jobs,
    ingest_failures,
    metadata,
    provider_state,
    sessions,
    sync_runs,
)
from aggregato.export import build_archive, restore_archive
from aggregato.main import create_app
from aggregato.sync.dispatch import build_dispatch
from aggregato.sync.scheduler import claim, due_providers

TOKEN = "phase-9-export-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "export-provider-secret-sentinel"
EXPORT = Path("tests/fixtures/fixture/log-two-pages.jsonl").resolve()
UPLOAD_NAME = "fixture.xml"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    data = tmp_path / "source"
    data.mkdir()
    app = create_app(load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data)}))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as session,
    ):
        yield session


def _app(client: httpx2.AsyncClient):
    transport = client._transport_for_url(httpx2.URL("http://test/"))
    assert isinstance(transport, httpx2.ASGITransport)
    return transport.app


async def _import_fixture(client: httpx2.AsyncClient) -> None:
    configured = await client.put(
        "/api/v1/providers/fixture/config",
        json={"path": str(EXPORT)},
    )
    assert configured.status_code == 200
    assert (await client.post("/api/v1/providers/fixture/enable")).status_code == 200
    response = await client.post(
        "/api/v1/providers/fixture/import",
        files={
            "file": (UPLOAD_NAME, await asyncio.to_thread(EXPORT.read_bytes), "application/xml")
        },
    )
    assert response.status_code == 202
    app = _app(client)
    due = await due_providers(app.state.engine, now=datetime.now(UTC))
    assert await claim(app.state.engine, "fixture", now=datetime.now(UTC))
    await build_dispatch(app.state.engine, app.state.config)(due[0])


async def test_export_restores_browsable_archive_without_syncs(
    client: httpx2.AsyncClient, tmp_path: Path
) -> None:
    await _import_fixture(client)
    exported = await client.get("/api/v1/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/octet-stream")
    archive = tmp_path / "archive.zip"
    archive.write_bytes(exported.content)
    restored_data = tmp_path / "restored"

    with zipfile.ZipFile(archive) as contents:
        config = contents.read("configuration.json")
        assert TOKEN.encode() not in config
        assert b"token" not in config

    restored_config = restore_archive(archive, restored_data)
    assert "api" in restored_config
    restored: Config = load_config(
        {"AGGREGATO_TOKEN": "fresh-token", "AGGREGATO_DATA": str(restored_data)}
    )
    app = create_app(restored)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://restored",
            headers={"Authorization": "Bearer fresh-token"},
        ) as fresh,
    ):
        entries = await fresh.get("/api/v1/entries", params={"include_subunits": True})
        assert entries.status_code == 200
        assert len(entries.json()["items"]) == 5


def test_malformed_restore_leaves_destination_retryable(tmp_path: Path) -> None:
    data_dir = tmp_path / "source"
    data_dir.mkdir()
    config = load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)})
    sync_engine = create_engine(make_url(config.database_url).set(drivername="sqlite"))
    metadata.create_all(sync_engine)
    sync_engine.dispose()
    valid = build_archive(config)
    malformed = tmp_path / "malformed.zip"
    with zipfile.ZipFile(malformed, "w") as contents:
        contents.writestr("aggregato.sqlite3", b"not a database")
        contents.writestr("configuration.json", b"{")

    destination = tmp_path / "restored"
    try:
        with pytest.raises(json.JSONDecodeError):
            restore_archive(malformed, destination)
        assert not destination.exists()
        restored = restore_archive(valid, destination)
        assert "api" in restored
        assert (destination / "aggregato.db").is_file()
    finally:
        valid.unlink(missing_ok=True)


async def test_export_requires_operator_for_public_and_readonly_gets(tmp_path: Path) -> None:
    data = tmp_path / "data"
    config = load_config(
        {
            "AGGREGATO_TOKEN": TOKEN,
            "AGGREGATO_READONLY_TOKEN": "readonly-export-token",
            "AGGREGATO_ALLOW_UNAUTHENTICATED_READONLY": "true",
            "AGGREGATO_DATA": str(data),
        }
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as public:
            assert (await public.get("/api/v1/export")).status_code == 403
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer readonly-export-token"},
        ) as readonly:
            assert (await readonly.get("/api/v1/export")).status_code == 403


async def test_export_reports_unsupported_database_as_a_problem(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(client)
    config = app.state.config.model_copy(
        update={"database_url": "postgresql+asyncpg://user:pass@localhost/archive"}
    )
    monkeypatch.setattr(app.state, "config", config)

    response = await client.get("/api/v1/export")

    assert response.status_code == 501
    assert response.json()["type"].endswith("portable-backup-unavailable")


async def test_in_memory_sqlite_export_returns_documented_problem(tmp_path: Path) -> None:
    config = load_config(
        {"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(tmp_path / "data")},
        db_overrides={"database_url": "sqlite+aiosqlite:///:memory:"},
    )
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://test",
            headers=AUTH,
        ) as client,
    ):
        export = await client.get("/api/v1/export")

    assert export.status_code == 501
    assert export.json()["type"].endswith("portable-backup-unavailable")
    assert "on-disk SQLite" in export.json()["detail"]


async def test_export_removes_credentials_from_every_archive_member(
    client: httpx2.AsyncClient, tmp_path: Path
) -> None:
    app = _app(client)
    configured = await client.put(
        "/api/v1/providers/listenbrainz/config",
        json={"username": "archive-reader", "token": SECRET},
    )
    assert configured.status_code == 200
    assert SECRET not in configured.text
    assert (await client.post("/api/v1/auth/session")).status_code == 204

    async with transaction(app.state.engine) as conn:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        run_result = await conn.execute(
            sync_runs.insert().values(
                provider_id="listenbrainz",
                lineage_id=uuid.uuid4(),
                attempt=1,
                mode="check",
                status="failed",
                phase="failed",
                started_at=now,
                finished_at=now,
                error_message=SECRET,
                log_excerpt=SECRET,
                log=SECRET,
                raw_responses=[{"body": SECRET}],
                cursor_before={"cursor": SECRET},
                cursor_after={"cursor": SECRET},
            )
        )
        run_id = int(run_result.inserted_primary_key[0])
        assert await conn.scalar(select(func.count()).select_from(sessions)) == 1
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "listenbrainz")
            .values(
                cursor={"cursor": SECRET},
                kv={"token": SECRET},
                now_playing_item={"token": SECRET},
            )
        )
        await conn.execute(
            import_jobs.insert().values(
                provider_id="listenbrainz",
                path=SECRET,
                created_at=now,
            )
        )
        await conn.execute(
            ingest_failures.insert().values(
                provider_id="fixture",
                sync_run_id=run_id,
                native_id=SECRET,
                raw_payload={"token": SECRET},
                error=SECRET,
                stage="normalize",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    exported = await client.get("/api/v1/export")
    assert exported.status_code == 200
    archive_path = tmp_path / "sanitized.zip"
    archive_path.write_bytes(exported.content)
    with zipfile.ZipFile(archive_path) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    assert all(SECRET.encode() not in content for content in members.values())

    snapshot = tmp_path / "snapshot.sqlite3"
    snapshot.write_bytes(members["aggregato.sqlite3"])
    with closing(sqlite3.connect(snapshot)) as database:
        assert database.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM import_jobs").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM ingest_failures").fetchone() == (0,)
        settings = json.loads(
            database.execute("SELECT config FROM providers WHERE id = 'listenbrainz'").fetchone()[0]
        )
        assert settings == {"username": "archive-reader"}
        assert database.execute(
            "SELECT enabled, status, last_error FROM providers WHERE id = 'listenbrainz'"
        ).fetchone() == (0, "disabled", None)
        state = database.execute(
            "SELECT cursor, kv, now_playing_item, now_playing_next_poll_at "
            "FROM provider_state WHERE provider_id = 'listenbrainz'"
        ).fetchone()
        assert state == (None, "{}", None, None)
        diagnostics = database.execute(
            "SELECT error_message, log_excerpt, log, raw_responses, cursor_before, cursor_after "
            "FROM sync_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert diagnostics == (None, None, None, None, None, None)


def test_failed_and_repeated_archive_builds_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "source"
    data_dir.mkdir()
    database_secret = "archive-db-url-secret"
    readonly_secret = "archive-readonly-secret"
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        json.dumps(
            {
                "database_url": (
                    f"sqlite+aiosqlite:///{data_dir / 'aggregato.db'}?password={database_secret}"
                ),
                "providers": {"listenbrainz": {"username": "archive-reader", "token": SECRET}},
                "api": {"readonly_token": readonly_secret},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(
        {"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)}, config_file=config_file
    )
    sync_engine = create_engine(make_url(config.database_url).set(drivername="sqlite", query={}))
    metadata.create_all(sync_engine)
    sync_engine.dispose()

    created: list[tuple[int, str]] = []
    original_mkstemp = export_module.tempfile.mkstemp

    def record_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        result = original_mkstemp(*args, **kwargs)
        created.append(result)
        return result

    def fail_zip(*args: object, **kwargs: object) -> None:
        raise RuntimeError("zip creation failed")

    monkeypatch.setattr(export_module.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(export_module.zipfile, "ZipFile", fail_zip)
    with pytest.raises(RuntimeError, match="zip creation failed"):
        build_archive(config)

    descriptor, filename = created[0]
    assert not Path(filename).exists()
    with pytest.raises(OSError):
        os.fstat(descriptor)

    monkeypatch.undo()
    for _ in range(3):
        archive = build_archive(config)
        try:
            assert archive.is_file()
            with zipfile.ZipFile(archive) as contents:
                members = [contents.read(info) for info in contents.infolist()]
                archive_config = json.loads(contents.read("configuration.json"))
            assert all(
                all(
                    secret.encode() not in member
                    for secret in (SECRET, database_secret, readonly_secret, TOKEN)
                )
                for member in members
            )
            assert archive_config["providers"]["listenbrainz"]["settings"] == {
                "username": "archive-reader"
            }
            assert "database_url" not in archive_config
            assert "token" not in archive_config["api"]
            assert "readonly_token" not in archive_config["api"]
        finally:
            archive.unlink(missing_ok=True)


async def test_settings_report_storage_and_disable_image_cache(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v1/settings")
    assert response.status_code == 200
    assert response.json()["backup_supported"] is True
    assert (
        response.json()["failure_run_retention_days"]
        > response.json()["success_run_retention_days"]
    )
    changed = await client.patch("/api/v1/settings", json={"image_cache_enabled": False})
    assert changed.status_code == 200
    assert changed.json()["backup_supported"] is True
    assert changed.json()["image_cache_enabled"] is False
    image = await client.get("/api/v1/media/image/not-a-source")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/gif"
