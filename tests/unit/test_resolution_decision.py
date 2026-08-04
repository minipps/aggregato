"""Resolution decisions preserve their queue-row audit snapshot."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from aggregato.config import load_config
from aggregato.db.schema import metadata, provider_items, resolution_queue, works
from aggregato.db.search import sync_create_search_index
from aggregato.main import create_app

TOKEN = "resolution-decision-token"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    data = tmp_path / "data"
    data.mkdir()
    app = create_app(
        load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data)}), run_migrations=False
    )
    winner, loser = uuid.UUID("dd0529d2-a6b2-4bf5-8ce9-6870e725196d"), uuid.uuid4()
    async with app.state.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.run_sync(sync_create_search_index)
        await conn.execute(
            works.insert(),
            [
                {
                    "id": work_id,
                    "media_type": "film",
                    "title": title,
                    "sort_title": title.lower(),
                    "metadata": {},
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for work_id, title in ((winner, "Winner"), (loser, "Loser"))
            ],
        )
        await conn.execute(
            provider_items.insert().values(
                id=1,
                provider_id="fixture",
                native_id="loser",
                work_id=loser,
                title_as_given="Loser",
                raw_payload={},
                schema_version=1,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        await conn.execute(
            resolution_queue.insert().values(
                id=1,
                subject="work",
                provider_id="fixture",
                payload_ref=1,
                candidates=[{"id": str(winner), "reason": "same title"}],
                proposed={},
                created_at=NOW,
            )
        )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as value,
    ):
        yield value


async def test_linked_decision_serializes_the_queue_snapshot(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/resolution-queue/1/decide",
        json={"decision": "linked", "target_id": "dd0529d2-a6b2-4bf5-8ce9-6870e725196d"},
    )

    assert response.status_code == 200
    assert response.json()["subject"] == "work"
