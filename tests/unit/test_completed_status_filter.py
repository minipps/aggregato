"""``GET /entries?status=completed`` expands to every kind that finished the work."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from aggregato.config import load_config
from aggregato.db.schema import entries, metadata, provider_items, works
from aggregato.domain.enums import COMPLETED_KINDS, EntryKind
from aggregato.main import create_app

TOKEN = "completed-status-token"
NOW = datetime(2026, 7, 30, tzinfo=UTC)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    data = tmp_path / "data"
    data.mkdir()
    app = create_app(
        load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data)}), run_migrations=False
    )
    work_id = uuid.uuid4()
    async with app.state.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(
            works.insert(),
            {
                "id": work_id,
                "media_type": "film",
                "title": "A work logged every way",
                "sort_title": "a work logged every way",
                "created_at": NOW,
                "updated_at": NOW,
            },
        )
        await conn.execute(
            provider_items.insert(),
            {
                "id": 1,
                "provider_id": "fixture",
                "native_id": "item",
                "work_id": work_id,
                "title_as_given": "A work logged every way",
                "raw_payload": {},
                "schema_version": 1,
                "first_seen_at": NOW,
                "last_seen_at": NOW,
            },
        )
        # One entry per kind, so the filter's exclusions are as visible as its inclusions.
        await conn.execute(
            entries.insert(),
            [
                {
                    "work_id": work_id,
                    "provider_id": "fixture",
                    "provider_item_id": 1,
                    "native_id": f"entry-{kind}",
                    "kind": str(kind),
                    "logged_at": NOW,
                    "logged_precision": "exact",
                    "subject_ref": None,
                    "ingested_at": NOW,
                }
                for kind in EntryKind
            ],
        )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as value,
    ):
        value.headers["Authorization"] = f"Bearer {TOKEN}"
        yield value


async def test_completed_selects_the_finished_kinds_and_nothing_else(
    client: httpx2.AsyncClient,
) -> None:
    response = await client.get("/api/v1/entries?status=completed")

    assert response.status_code == 200
    kinds = {item["kind"] for item in response.json()["items"]}
    assert kinds == {str(kind) for kind in COMPLETED_KINDS}
    assert {"progress", "drop"}.isdisjoint(kinds)


async def test_status_and_kind_intersect_rather_than_widen(client: httpx2.AsyncClient) -> None:
    """Both filters AND, so ``status`` can never smuggle back a kind ``kind`` excluded."""
    response = await client.get("/api/v1/entries?status=completed&kind=drop")

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_an_unknown_status_is_a_422_not_a_silent_full_page(
    client: httpx2.AsyncClient,
) -> None:
    response = await client.get("/api/v1/entries?status=in_progress")

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
