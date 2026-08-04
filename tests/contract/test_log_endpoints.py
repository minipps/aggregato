"""Contract smoke tests for the  log resources ."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from aggregato.config import load_config
from aggregato.db.schema import metadata
from aggregato.main import create_app

TOKEN = "log-contract-token"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    data = tmp_path / "data"
    data.mkdir()
    config = load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data)})
    app = create_app(config, run_migrations=False)
    async with app.state.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as value,
    ):
        value.headers["Authorization"] = f"Bearer {TOKEN}"
        yield value


@pytest.mark.parametrize("path", ["/entries", "/works", "/opinions"])
async def test_log_collections_have_the_contract_page_shape(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(f"/api/v1{path}")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_log_resources_require_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/entries", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


async def test_unknown_work_is_a_problem_detail(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/works/not-a-uuid")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
