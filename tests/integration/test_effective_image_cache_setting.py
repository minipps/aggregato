from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from aggregato.config import load_config
from aggregato.db.engine import transaction
from aggregato.db.retention import get_settings, update_settings
from aggregato.db.schema import settings
from aggregato.main import create_app

TOKEN = "effective-image-cache-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def disabled_cache_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx2.AsyncClient, FastAPI]]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text("image_cache_enabled: false\n", encoding="utf-8")
    config = load_config(
        {"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)},
        config_file=config_file,
    )
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as client,
    ):
        yield client, app


async def test_config_hard_disables_cache_and_preserves_database_preference(
    disabled_cache_client: tuple[httpx2.AsyncClient, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app = disabled_cache_client
    engine = app.state.engine
    await update_settings(
        engine, {"image_cache_enabled": True}, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    response = await client.get("/api/v1/settings")
    assert response.json()["image_cache_enabled"] is False
    assert response.json()["image_cache_configured_enabled"] is False

    changed = await client.patch(
        "/api/v1/settings",
        json={"image_cache_enabled": True, "raw_payload_retention_days": 37},
    )
    assert changed.status_code == 200
    assert changed.json()["image_cache_enabled"] is False
    assert changed.json()["image_cache_configured_enabled"] is False
    assert changed.json()["raw_payload_retention_days"] == 37
    assert (await get_settings(engine))["image_cache_enabled"] is True

    async with transaction(engine) as conn:
        stored_preference = await conn.scalar(
            select(settings.c.value).where(settings.c.key == "image_cache_enabled")
        )
    assert stored_preference is True

    async def forbidden_cache(*args: object, **kwargs: object) -> None:
        raise AssertionError("hard-disabled image caching must not access the cache")

    monkeypatch.setattr("aggregato.api.routes.images.cached_image", forbidden_cache)
    digest = hashlib.sha256(b"http://127.0.0.1/private-image.png").hexdigest()
    image = await client.get(f"/api/v1/media/image/{digest}")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/gif"
