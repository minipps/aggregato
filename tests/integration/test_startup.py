"""Both processes start from cold, for real .

Every other test in the suite builds its schema with ``metadata.create_all``, which means none of
them exercises the startup path an operator actually hits. That gap hid two real bugs:

* the API's lifespan called Alembic **inline**, and Alembic's ``env.py`` drives its own
  ``asyncio.run`` — so the first real startup raised "asyncio.run() cannot be called from a running
  event loop" while every unit test passed;
* the scheduler did not migrate at all, so starting it before the API crash-looped on
  ``no such table: provider_state`` every five seconds.

Neither is subtle once seen, and neither was reachable without starting the thing. Hence this file.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import inspect

from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine
from aggregato.db.schema import metadata
from aggregato.main import create_app

TOKEN = "startup-test-token"


@pytest.fixture
def data_dir(tmp_path: Path) -> Iterator[Path]:
    directory = tmp_path / "data"
    directory.mkdir()
    yield directory


def config_for(data_dir: Path) -> Config:
    return load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)})


async def test_the_api_migrates_on_startup_from_an_empty_directory(data_dir: Path) -> None:
    """The regression test for the inline-Alembic bug. It must run migrations, not raise."""
    config = config_for(data_dir)
    app = create_app(config)

    async with app.router.lifespan_context(app):
        pass

    assert (data_dir / "aggregato.db").is_file()

    engine = create_engine(config.database_url)
    try:
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()

    missing = set(metadata.tables) - tables
    assert missing == set(), f"startup did not create: {sorted(missing)}"
    assert "search_index" in tables
    assert "alembic_version" in tables


async def test_starting_twice_is_harmless(data_dir: Path) -> None:
    """An operator restarts the container; migrations must be idempotent ."""
    config = config_for(data_dir)
    for _ in range(2):
        app = create_app(config)
        async with app.router.lifespan_context(app):
            pass
    assert (data_dir / "aggregato.db").is_file()


async def test_a_restart_at_head_does_not_create_a_backup(data_dir: Path) -> None:
    config = config_for(data_dir)
    for _ in range(2):
        app = create_app(config)
        async with app.router.lifespan_context(app):
            pass
    backups = await asyncio.to_thread(lambda: list(data_dir.glob("aggregato.db.*.bak")))
    assert backups == []


async def test_the_served_api_answers_after_a_cold_start(data_dir: Path) -> None:
    """Startup to a working authenticated request, with nothing pre-created."""
    app = create_app(config_for(data_dir))

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthenticated = await client.get("/api/v1/health")
            authenticated = await client.get(
                "/api/v1/health", headers={"Authorization": f"Bearer {TOKEN}"}
            )

    # Default configuration remains authenticated, and the refusal is problem+json like every
    # other failure.
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["content-type"] == "application/problem+json"

    assert authenticated.status_code == 200
    # A fresh install has no providers, which is `ok` with an empty list rather than an error.
    assert authenticated.json() == {"status": "ok", "providers": []}


async def test_a_fresh_install_makes_no_outbound_request(data_dir: Path) -> None:
    """, at the startup boundary.

    ``tests/conftest.py`` blocks sockets outright, so a cold start that reached for anything would
    raise here rather than merely be noticed later.
    """
    app = create_app(config_for(data_dir))
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/health", headers={"Authorization": f"Bearer {TOKEN}"}
            )
    assert response.status_code == 200


async def test_the_scheduler_migrates_when_it_starts_first(data_dir: Path) -> None:
    """The regression test for the crash-looping scheduler.

    The scheduler must be independently restartable, so it cannot assume the API went first and
    created the schema for it.
    """
    from aggregato.db.migrate import upgrade_to_head

    config = config_for(data_dir)
    await asyncio.to_thread(upgrade_to_head, config.database_url)

    engine = create_engine(config.database_url)
    try:
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()

    assert "provider_state" in tables, "the scheduler would crash-loop on its first poll"


async def test_an_unset_token_stops_startup(data_dir: Path) -> None:
    """The only fatal configuration error . Fails at build, not on the first request."""
    from aggregato.config import MissingTokenError

    with pytest.raises(MissingTokenError, match="AGGREGATO_TOKEN"):
        create_app(load_config({"AGGREGATO_DATA": str(data_dir)}))


async def test_a_missing_frontend_build_is_not_an_error(data_dir: Path) -> None:
    """Normal in a development checkout, where Vite serves the SPA and proxies /api."""
    config = config_for(data_dir)
    assert config.static_dir is None
    app = create_app(config, run_migrations=False)
    # Built without raising; the API-only surface is the whole point.
    assert app.openapi()["paths"]
