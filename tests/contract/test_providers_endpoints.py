"""``/providers`` against the contract (T050, T056).

The behaviour worth pinning here is not the happy path — it is that enabling is the *only* way
anything starts, that a broken provider is contained rather than fatal, and that "sync now" cannot
race itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update

from aggregato.config import Config, load_config
from aggregato.db.engine import transaction
from aggregato.db.schema import provider_state, providers
from aggregato.domain.enums import ProviderStatus
from aggregato.main import create_app

TOKEN = "providers-contract-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FIXTURE = (Path(__file__).parent.parent / "fixtures/fixture/log-two-pages.jsonl").resolve()


def config_for(data_dir: Path, **overrides: object) -> Config:
    return load_config(
        {"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)},
        db_overrides={"providers": {"fixture": {"path": str(FIXTURE)}}, **overrides},
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = create_app(config_for(data_dir))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.headers.update(AUTH)
            yield c


async def _engine_for(client: httpx.AsyncClient) -> object:
    """The app's own engine, so a test can inspect state the API does not expose."""
    transport = client._transport_for_url(httpx.URL("http://test/"))
    assert isinstance(transport, httpx.ASGITransport)
    return transport.app.state.engine  # type: ignore[union-attr]


# --- Listing -----------------------------------------------------------------------------------


async def test_listing_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/providers", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


async def test_listing_returns_the_contract_shape(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/providers")
    assert response.status_code == 200
    body = response.json()
    assert body, "no providers discovered"

    required = {
        "id",
        "name",
        "enabled",
        "status",
        "acquisition",
        "reviewed",
        "capabilities",
        "media_types",
    }
    for provider in body:
        assert required <= set(provider), f"missing {required - set(provider)}"


async def test_a_discovered_provider_is_listed_before_it_is_enabled(
    client: httpx.AsyncClient,
) -> None:
    """Discovery is not activation. Listing must work with no database rows at all (SC-013)."""
    body = (await client.get("/api/v1/providers")).json()
    assert {p["id"] for p in body} >= {"fixture"}
    assert all(p["enabled"] is False for p in body)


async def test_bundled_providers_are_marked_reviewed(client: httpx.AsyncClient) -> None:
    """FR-041: ``reviewed`` false is for drop-in development providers, which the UI labels."""
    body = (await client.get("/api/v1/providers")).json()
    assert all(p["reviewed"] is True for p in body)


async def test_provider_config_schema_is_declared_by_the_provider(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/providers/anilist/config-schema")
    assert response.status_code == 200
    schema = response.json()
    assert schema["properties"]["username"]["description"]
    assert schema["properties"]["token"]["writeOnly"] is True


async def test_config_schema_of_an_unknown_provider_is_a_404_problem(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/providers/not-a-provider/config-schema")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


# --- Enable and disable -------------------------------------------------------------------------


async def test_enabling_makes_a_provider_enabled_and_due(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/providers/fixture/enable")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["status"] == "idle"
    # Due immediately, so an operator who just enabled a platform sees something happen.
    assert body["next_run_at"] is not None


async def test_enabling_twice_is_harmless(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/providers/fixture/enable")
    second = await client.post("/api/v1/providers/fixture/enable")
    assert second.status_code == 200
    assert second.json()["enabled"] is True


async def test_disabling_stops_it_being_due_without_deleting_anything(
    client: httpx.AsyncClient,
) -> None:
    """Disabling is not a way to lose history (FR-024)."""
    await client.post("/api/v1/providers/fixture/enable")
    response = await client.post("/api/v1/providers/fixture/disable")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["status"] == "disabled"
    assert body["next_run_at"] is None


async def test_enabling_an_unknown_provider_is_a_404_problem(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/providers/not-a-provider/enable")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["title"]


# --- Sync now ----------------------------------------------------------------------------------


async def test_sync_now_queues_and_returns_a_lineage(client: httpx.AsyncClient) -> None:
    """202, because the run is queued rather than performed — the API cannot spawn it (R1)."""
    await client.post("/api/v1/providers/fixture/enable")
    response = await client.post("/api/v1/providers/fixture/sync")

    assert response.status_code == 202
    assert response.json()["lineage_id"]


async def test_sync_now_makes_the_provider_due(client: httpx.AsyncClient) -> None:
    """The whole mechanism: the API writes next_run_at, the scheduler notices."""
    from datetime import UTC, datetime

    from aggregato.sync.scheduler import due_providers

    await client.post("/api/v1/providers/fixture/enable")
    await client.post("/api/v1/providers/fixture/sync")

    engine = await _engine_for(client)
    due = await due_providers(engine, now=datetime.now(UTC))  # type: ignore[arg-type]
    assert [d.provider_id for d in due] == ["fixture"]


async def test_a_full_sync_clears_the_cursor(client: httpx.AsyncClient) -> None:
    """A full run that resumed from a cursor would not be a full run."""
    await client.post("/api/v1/providers/fixture/enable")
    engine = await _engine_for(client)

    async with transaction(engine) as conn:  # type: ignore[arg-type]
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "fixture")
            .values(cursor={"next_page": 7})
        )

    await client.post("/api/v1/providers/fixture/sync", json={"mode": "full"})

    async with transaction(engine) as conn:  # type: ignore[arg-type]
        cursor = (await conn.execute(select(provider_state.c.cursor))).scalar_one()
    assert cursor is None


async def test_an_incremental_sync_keeps_the_cursor(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/providers/fixture/enable")
    engine = await _engine_for(client)

    async with transaction(engine) as conn:  # type: ignore[arg-type]
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "fixture")
            .values(cursor={"next_page": 7})
        )

    await client.post("/api/v1/providers/fixture/sync")

    async with transaction(engine) as conn:  # type: ignore[arg-type]
        cursor = (await conn.execute(select(provider_state.c.cursor))).scalar_one()
    assert cursor == {"next_page": 7}


async def test_syncing_a_disabled_provider_is_a_409(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/providers/fixture/sync")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"


async def test_syncing_while_a_run_is_in_flight_is_a_409(client: httpx.AsyncClient) -> None:
    """Two concurrent runs of one provider would race on its cursor."""
    await client.post("/api/v1/providers/fixture/enable")
    engine = await _engine_for(client)

    async with transaction(engine) as conn:  # type: ignore[arg-type]
        await conn.execute(
            update(providers)
            .where(providers.c.id == "fixture")
            .values(status=str(ProviderStatus.SYNCING))
        )

    response = await client.post("/api/v1/providers/fixture/sync")
    assert response.status_code == 409
    assert "already syncing" in response.json()["detail"]


async def test_an_unknown_sync_mode_is_rejected(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/providers/fixture/enable")
    response = await client.post("/api/v1/providers/fixture/sync", json={"mode": "sideways"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"


async def test_syncing_an_unknown_provider_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/providers/nope/sync")
    assert response.status_code == 404


# --- Check -------------------------------------------------------------------------------------


async def test_check_on_a_provider_that_never_ran_says_so(client: httpx.AsyncClient) -> None:
    """Not-yet-known is different from broken, and the response must not conflate them."""
    response = await client.post("/api/v1/providers/fixture/check")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_class"] is None
    assert "not run yet" in body["detail"]


async def test_check_reports_the_last_run_error_class(client: httpx.AsyncClient) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from aggregato.db.schema import sync_runs

    engine = await _engine_for(client)
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        await conn.execute(
            sync_runs.insert().values(
                provider_id="fixture",
                lineage_id=uuid4(),
                attempt=1,
                mode="incremental",
                status="failed",
                started_at=datetime.now(UTC),
                error_class="auth",
                error_message="token rejected",
            )
        )

    body = (await client.post("/api/v1/providers/fixture/check")).json()
    assert body["ok"] is False
    assert body["error_class"] == "auth"
    assert body["detail"] == "token rejected"


async def test_check_on_an_unknown_provider_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/v1/providers/nope/check")).status_code == 404


# --- Containment -------------------------------------------------------------------------------


async def test_an_invalid_provider_config_does_not_break_listing(tmp_path: Path) -> None:
    """FR-025: a broken provider is contained. The service starts and everything else works.

    This is the containment guarantee at the API boundary — a misconfigured platform must cost the
    operator that platform, not their archive.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = load_config(
        {"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)},
        # `path` is required by FixtureConfig, so omitting it is a genuine validation failure.
        db_overrides={"providers": {"fixture": {"wrong_key": 1}}},
    )
    app = create_app(config)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.headers.update(AUTH)
            listing = await c.get("/api/v1/providers")
            health = await c.get("/api/v1/health")

    # The service is up and answering, which is the whole claim.
    assert listing.status_code == 200
    assert health.status_code == 200
