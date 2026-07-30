"""Cross-origin preflight is answered for listed origins only, and only when one is listed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from aggregato.api.deps import CSRF_HEADER
from aggregato.config import ConfigError, load_config
from aggregato.db.schema import metadata
from aggregato.main import create_app

TOKEN = "cors-token"
ORIGIN = "https://log.example.com"
PREFLIGHT = {"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}


@asynccontextmanager
async def app_client(tmp_path: Path, **env: str) -> AsyncIterator[httpx.AsyncClient]:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    config = load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data), **env})
    app = create_app(config, run_migrations=False)
    async with app.state.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def test_no_cors_middleware_without_a_configured_origin(tmp_path: Path) -> None:
    """The default deployment is same-origin, so nothing is mounted and OPTIONS stays a 405."""
    async with app_client(tmp_path) as client:
        response = await client.options("/api/v1/entries", headers=PREFLIGHT)

    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers


async def test_a_listed_origin_is_preflighted_without_credentials(tmp_path: Path) -> None:
    """A browser sends no Authorization or cookie on a preflight, so it must not need one.

    This is why it is middleware and not a route: the app-wide ``require_auth`` dependency would
    401 an unauthenticated OPTIONS, and the browser would never send the real request.
    """
    async with app_client(tmp_path, AGGREGATO_CORS_ORIGINS=ORIGIN) as client:
        response = await client.options("/api/v1/entries", headers=PREFLIGHT)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    allowed = response.headers["access-control-allow-headers"].lower()
    # Without this the preflight passes and every cookie-authenticated write then fails.
    assert CSRF_HEADER.lower() in allowed


async def test_an_unlisted_origin_is_not_answered(tmp_path: Path) -> None:
    async with app_client(tmp_path, AGGREGATO_CORS_ORIGINS=ORIGIN) as client:
        response = await client.options(
            "/api/v1/entries",
            headers={"Origin": "https://elsewhere.example", "Access-Control-Request-Method": "GET"},
        )

    assert "access-control-allow-origin" not in response.headers


async def test_several_origins_come_from_one_comma_separated_variable(tmp_path: Path) -> None:
    second = "http://localhost:4173"
    async with app_client(tmp_path, AGGREGATO_CORS_ORIGINS=f"{ORIGIN}, {second}") as client:
        response = await client.options(
            "/api/v1/entries",
            headers={"Origin": second, "Access-Control-Request-Method": "GET"},
        )

    assert response.headers["access-control-allow-origin"] == second


async def test_authentication_is_still_required_on_the_real_request(tmp_path: Path) -> None:
    """CORS decides who may ask. It does not decide who gets an answer (FR-032)."""
    async with app_client(tmp_path, AGGREGATO_CORS_ORIGINS=ORIGIN) as client:
        response = await client.get("/api/v1/entries", headers={"Origin": ORIGIN})

    assert response.status_code == 401


def test_a_wildcard_origin_is_refused(tmp_path: Path) -> None:
    """`*` with credentials is forbidden by CORS, so accepting it would break every request."""
    with pytest.raises(ConfigError, match=r"does not accept '\*'"):
        load_config(
            {
                "AGGREGATO_TOKEN": TOKEN,
                "AGGREGATO_DATA": str(tmp_path),
                "AGGREGATO_CORS_ORIGINS": "*",
            }
        )
