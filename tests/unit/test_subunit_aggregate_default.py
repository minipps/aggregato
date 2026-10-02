"""The named regression guard for  ."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from aggregato.config import load_config
from aggregato.db.schema import entries, metadata, provider_items, works
from aggregato.main import create_app

TOKEN = "subunit-statistics-token"
NOW = datetime(2026, 7, 30, tzinfo=UTC)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    data = tmp_path / "data"
    data.mkdir()
    app = create_app(
        load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data)}), run_migrations=False
    )
    whole_work = uuid.uuid4()
    episode_work = uuid.uuid4()
    async with app.state.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(
            works.insert(),
            [
                {
                    "id": whole_work,
                    "media_type": "film",
                    "title": "Whole work",
                    "sort_title": "whole work",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": episode_work,
                    "media_type": "tv",
                    "title": "Season with episodes",
                    "sort_title": "season with episodes",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )
        await conn.execute(
            provider_items.insert(),
            [
                {
                    "id": 1,
                    "provider_id": "fixture",
                    "native_id": "whole",
                    "work_id": whole_work,
                    "title_as_given": "Whole work",
                    "raw_payload": {},
                    "schema_version": 1,
                    "first_seen_at": NOW,
                    "last_seen_at": NOW,
                },
                {
                    "id": 2,
                    "provider_id": "fixture",
                    "native_id": "episode",
                    "work_id": episode_work,
                    "title_as_given": "Season with episodes",
                    "raw_payload": {},
                    "schema_version": 1,
                    "first_seen_at": NOW,
                    "last_seen_at": NOW,
                },
            ],
        )
        await conn.execute(
            entries.insert(),
            [
                {
                    "work_id": whole_work,
                    "provider_id": "fixture",
                    "provider_item_id": 1,
                    "native_id": "whole-entry",
                    "kind": "watch",
                    "logged_at": NOW,
                    "logged_precision": "exact",
                    "subject_ref": None,
                    "ingested_at": NOW,
                },
                {
                    "work_id": episode_work,
                    "provider_id": "fixture",
                    "provider_item_id": 2,
                    "native_id": "episode-entry",
                    "kind": "watch",
                    "logged_at": NOW,
                    "logged_precision": "exact",
                    "subject_ref": {"season": 1, "episode": 1},
                    "ingested_at": NOW,
                },
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


async def test_stats_exclude_subunit_entries_until_explicitly_requested(
    client: httpx2.AsyncClient,
) -> None:
    """The default protects whole-work statistics from episode/track activity corruption."""
    default = await client.get("/api/v1/stats/summary")
    opted_in = await client.get("/api/v1/stats/summary?include_subunits=true")

    assert default.json()["total_entries"] == 1, (
        "default stats included a sub-unit entry and would corrupt whole-work statistics for "
        "episode- or track-level loggers"
    )
    assert opted_in.json()["total_entries"] == 2

    top_default = await client.get("/api/v1/stats/top?group=work")
    top_opted_in = await client.get("/api/v1/stats/top?group=work&include_subunits=true")
    assert [item["label"] for item in top_default.json()["items"]] == ["Whole work"]
    assert len(top_opted_in.json()["items"]) == 2
