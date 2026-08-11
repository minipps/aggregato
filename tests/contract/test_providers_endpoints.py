"""``/providers`` against the contract .

The behaviour worth pinning here is not the happy path — it is that enabling is the *only* way
anything starts, that a broken provider is contained rather than fatal, and that "sync now" cannot
race itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from aggregato.config import Config, load_config
from aggregato.db.engine import transaction
from aggregato.db.schema import provider_state, providers, sync_runs
from aggregato.domain.enums import FetchMode, ProviderStatus, RunStatus
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
    """Discovery is not activation, and test-only providers are not advertised ."""
    body = (await client.get("/api/v1/providers")).json()
    assert "fixture" not in {p["id"] for p in body}
    assert all(p["enabled"] is False for p in body)


async def test_bundled_providers_are_marked_reviewed(client: httpx.AsyncClient) -> None:
    """: ``reviewed`` false is for drop-in development providers, which the UI labels."""
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


async def test_configure_provider_persists_validated_settings_without_enabling_it(
    client: httpx.AsyncClient,
) -> None:
    """Web configuration is durable but cannot start a provider without a separate enable action."""
    response = await client.put("/api/v1/providers/fixture/config", json={"path": str(FIXTURE)})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    engine = await _engine_for(client)
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        saved = (
            await conn.execute(select(providers.c.config).where(providers.c.id == "fixture"))
        ).scalar_one()
    assert saved == {"path": str(FIXTURE)}


async def test_listing_shows_only_non_sensitive_current_provider_settings(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/v1/providers/anilist/config",
        json={
            "username": "dawn",
            "token": "private-token",
            "api_url": "https://example.test/graphql",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current_settings"] == {
        "username": "dawn",
        "api_url": "https://example.test/graphql",
    }
    assert "private-token" not in response.text

    listed = (await client.get("/api/v1/providers")).json()
    anilist = next(provider for provider in listed if provider["id"] == "anilist")
    assert anilist["current_settings"] == body["current_settings"]
    assert "token" not in anilist["current_settings"]


async def test_invalid_provider_config_is_rejected_without_creating_provider_state(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put("/api/v1/providers/fixture/config", json={"wrong_key": 1})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    engine = await _engine_for(client)
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        assert (
            await conn.execute(select(providers.c.id).where(providers.c.id == "fixture"))
        ).first() is None


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
    """Disabling is not a way to lose history ."""
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


async def test_sync_status_reports_queue_and_live_run_progress(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/api/v1/providers/fixture/enable")
    await client.post("/api/v1/providers/fixture/sync")
    queued = (await client.get("/api/v1/sync/status")).json()
    fixture_state = next(item for item in queued["providers"] if item["id"] == "fixture")
    assert fixture_state["requested_mode"] == "incremental"
    assert fixture_state["requested_lineage_id"]
    assert queued["runs"] == []

    engine = await _engine_for(client)
    run_id = uuid4()
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        await conn.execute(
            update(providers)
            .where(providers.c.id == "fixture")
            .values(status=str(ProviderStatus.SYNCING))
        )
        await conn.execute(
            sync_runs.insert().values(
                provider_id="fixture",
                lineage_id=run_id,
                attempt=1,
                mode=str(FetchMode.INCREMENTAL),
                status=str(RunStatus.RUNNING),
                phase="fetching",
                started_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
                updated_at=datetime(2026, 8, 11, 12, 0, 1, tzinfo=UTC),
                items_seen=7,
                items_written=0,
                items_failed=1,
                progress_total=None,
                checkpoint_count=2,
                last_checkpoint_at=datetime(2026, 8, 11, 12, 0, 1, tzinfo=UTC),
                cursor_after={"page": 2},
            )
        )

    body = (await client.get("/api/v1/sync/status")).json()
    run = next(item for item in body["runs"] if item["provider_id"] == "fixture")
    assert run["status"] == "running"
    assert run["phase"] == "fetching"
    assert run["items_seen"] == 7
    assert run["items_failed"] == 1
    assert run["checkpoint_count"] == 2
    assert run["cursor_after"] == {"page": 2}
    assert run["progress_percent"] is None


async def test_sync_now_queues_and_returns_a_lineage(client: httpx.AsyncClient) -> None:
    """202, because the run is queued rather than performed — the API cannot spawn it ."""
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


async def test_a_full_sync_request_reaches_the_dispatcher(client: httpx.AsyncClient) -> None:
    """Clearing the cursor is not enough on its own.

    The scheduler has no mode of its own — it is a timestamp column — so a run dispatched from it
    was always ``incremental``, whatever the operator asked for. That downgrade is invisible: the
    cursor clearing makes most providers re-read anyway, while the run is *recorded* as incremental
    and the full-run guards (window sanity, inferred deletes) never fire.
    """
    from datetime import UTC, datetime

    from aggregato.sync.scheduler import due_providers

    await client.post("/api/v1/providers/fixture/enable")
    await client.post("/api/v1/providers/fixture/sync", json={"mode": "full"})

    engine = await _engine_for(client)
    (due,) = await due_providers(engine, now=datetime.now(UTC))  # type: ignore[arg-type]
    assert due.requested_mode == "full"


async def test_an_incremental_sync_leaves_no_mode_request_behind(
    client: httpx.AsyncClient,
) -> None:
    """A scheduled run is incremental, so the ordinary case stores nothing to consume."""
    from datetime import UTC, datetime

    from aggregato.sync.scheduler import due_providers

    await client.post("/api/v1/providers/fixture/enable")
    await client.post("/api/v1/providers/fixture/sync")

    engine = await _engine_for(client)
    (due,) = await due_providers(engine, now=datetime.now(UTC))  # type: ignore[arg-type]
    assert due.requested_mode == "incremental"


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


# --- Credential check --------------------------------------------------------------------------


async def test_check_queues_a_disabled_provider_without_rescheduling_it(
    client: httpx.AsyncClient,
) -> None:
    """A diagnostic is an explicit worker job, not an enable or a schedule mutation."""
    await client.put("/api/v1/providers/fixture/config", json={"path": str(FIXTURE)})
    engine = await _engine_for(client)
    preserved_schedule = datetime(2099, 1, 1, tzinfo=UTC)
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "fixture")
            .values(next_run_at=preserved_schedule, cursor={"next_page": 7})
        )

    response = await client.post("/api/v1/providers/fixture/check")
    assert response.status_code == 202
    lineage_id = response.json()["lineage_id"]

    async with transaction(engine) as conn:  # type: ignore[arg-type]
        state = (
            await conn.execute(
                select(
                    provider_state.c.next_run_at,
                    provider_state.c.cursor,
                    provider_state.c.requested_mode,
                    provider_state.c.requested_lineage_id,
                ).where(provider_state.c.provider_id == "fixture")
            )
        ).one()
        provider = (
            await conn.execute(
                select(providers.c.enabled, providers.c.status).where(providers.c.id == "fixture")
            )
        ).one()
    assert provider.enabled is False
    assert provider.status == str(ProviderStatus.DISABLED)
    assert state.next_run_at.replace(tzinfo=UTC) == preserved_schedule
    assert state.cursor == {"next_page": 7}
    assert state.requested_mode == str(FetchMode.CHECK)
    assert str(state.requested_lineage_id) == lineage_id

    from aggregato.sync.scheduler import due_providers

    due = await due_providers(engine, now=datetime(2026, 1, 1, tzinfo=UTC))  # type: ignore[arg-type]
    assert [item.provider_id for item in due] == ["fixture"]
    assert (await client.post("/api/v1/providers/fixture/check")).status_code == 409


async def test_check_runs_in_isolation_and_is_not_sync_history(
    client: httpx.AsyncClient,
) -> None:
    await client.put("/api/v1/providers/fixture/config", json={"path": str(FIXTURE)})
    engine = await _engine_for(client)
    preserved_schedule = datetime(2099, 1, 1, tzinfo=UTC)
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == "fixture")
            .values(next_run_at=preserved_schedule, cursor={"next_page": 7})
        )

    queued = await client.post("/api/v1/providers/fixture/check")
    assert queued.status_code == 202

    from aggregato.sync.dispatch import build_dispatch
    from aggregato.sync.scheduler import claim, due_providers

    due = await due_providers(engine, now=datetime(2026, 1, 1, tzinfo=UTC))  # type: ignore[arg-type]
    assert await claim(engine, "fixture", now=datetime(2026, 1, 1, tzinfo=UTC))  # type: ignore[arg-type]
    transport = client._transport_for_url(httpx.URL("http://test/"))
    assert isinstance(transport, httpx.ASGITransport)
    await build_dispatch(engine, transport.app.state.config)(due[0])  # type: ignore[arg-type, union-attr]

    async with transaction(engine) as conn:  # type: ignore[arg-type]
        run = (
            await conn.execute(
                select(
                    sync_runs.c.mode,
                    sync_runs.c.status,
                    sync_runs.c.items_written,
                    sync_runs.c.items_failed,
                )
                .where(sync_runs.c.provider_id == "fixture")
                .order_by(sync_runs.c.id.desc())
                .limit(1)
            )
        ).one()
        state = (
            await conn.execute(
                select(
                    provider_state.c.next_run_at,
                    provider_state.c.cursor,
                    provider_state.c.requested_mode,
                ).where(provider_state.c.provider_id == "fixture")
            )
        ).one()
        provider = (
            await conn.execute(
                select(providers.c.enabled, providers.c.status).where(providers.c.id == "fixture")
            )
        ).one()
    assert run.mode == str(FetchMode.CHECK)
    assert run.status == str(RunStatus.SUCCESS)
    assert run.items_written == 0
    assert run.items_failed == 0
    assert state.next_run_at.replace(tzinfo=UTC) == preserved_schedule
    assert state.cursor == {"next_page": 7}
    assert state.requested_mode is None
    assert provider.enabled is False
    assert provider.status == str(ProviderStatus.DISABLED)

    latest = await client.get("/api/v1/providers/fixture/last-run")
    assert latest.status_code == 200
    assert latest.json()["status"] is None
    view = await client.put("/api/v1/providers/fixture/config", json={"path": str(FIXTURE)})
    assert view.json()["last_check"]["status"] == "success"


async def test_check_rejects_invalid_configuration(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/providers/fixture/enable")
    engine = await _engine_for(client)
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        await conn.execute(
            update(providers).where(providers.c.id == "fixture").values(config={"wrong_key": 1})
        )

    response = await client.post("/api/v1/providers/fixture/check")
    assert response.status_code == 422
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        requested = (
            await conn.execute(
                select(provider_state.c.requested_mode).where(
                    provider_state.c.provider_id == "fixture"
                )
            )
        ).scalar_one()
    assert requested is None


async def test_check_rejects_an_active_sync(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/providers/fixture/enable")
    engine = await _engine_for(client)
    async with transaction(engine) as conn:  # type: ignore[arg-type]
        await conn.execute(
            sync_runs.insert().values(
                provider_id="fixture",
                lineage_id=uuid4(),
                attempt=1,
                mode=str(FetchMode.INCREMENTAL),
                status=str(RunStatus.RUNNING),
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    response = await client.post("/api/v1/providers/fixture/check")
    assert response.status_code == 409


# --- Latest recorded run -----------------------------------------------------------------------


async def test_latest_run_on_a_provider_that_never_ran_says_so(client: httpx.AsyncClient) -> None:
    """Not-yet-known is different from broken, and the response must not conflate them."""
    response = await client.get("/api/v1/providers/fixture/last-run")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] is None
    assert body["error_class"] is None
    assert "not run yet" in body["detail"]


async def test_latest_run_reports_the_last_run_error_class(client: httpx.AsyncClient) -> None:
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

    body = (await client.get("/api/v1/providers/fixture/last-run")).json()
    assert body["status"] == "failed"
    assert body["error_class"] == "auth"
    assert body["detail"] == "token rejected"


async def test_latest_run_on_an_unknown_provider_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/providers/nope/last-run")).status_code == 404


# --- Containment -------------------------------------------------------------------------------


async def test_an_invalid_provider_config_does_not_break_listing(tmp_path: Path) -> None:
    """: a broken provider is contained. The service starts and everything else works.

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
