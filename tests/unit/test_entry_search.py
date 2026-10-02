"""Search through both indexed record types using the real entries API route."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from aggregato.config import load_config
from aggregato.db.schema import entries, metadata, opinions, provider_items, works
from aggregato.db.search import SearchKind, create_search_index, index_document
from aggregato.main import create_app

TOKEN = "entry-search-token"
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    data = tmp_path / "data"
    data.mkdir()
    app = create_app(
        load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data)}), run_migrations=False
    )
    records = [
        (uuid.uuid4(), 1, "Echo title", None, NOW),
        (
            uuid.uuid4(),
            2,
            "Unrelated title",
            "An echo appears in this review",
            NOW - timedelta(minutes=1),
        ),
        (uuid.uuid4(), 3, "Comet", None, NOW - timedelta(minutes=2)),
    ]
    async with app.state.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await create_search_index(conn)
        for work_id, item_id, title, review, logged_at in records:
            await conn.execute(
                works.insert().values(
                    id=work_id,
                    media_type="film",
                    title=title,
                    sort_title=title.casefold(),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await conn.execute(
                provider_items.insert().values(
                    id=item_id,
                    provider_id="fixture",
                    native_id=f"item-{item_id}",
                    work_id=work_id,
                    title_as_given=title,
                    raw_payload={},
                    schema_version=1,
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                )
            )
            await conn.execute(
                entries.insert().values(
                    work_id=work_id,
                    provider_id="fixture",
                    provider_item_id=item_id,
                    native_id=f"event-{item_id}",
                    kind="watch",
                    logged_at=logged_at,
                    logged_precision="exact",
                    ingested_at=logged_at,
                )
            )
            if review is not None:
                await conn.execute(
                    opinions.insert().values(
                        work_id=work_id,
                        provider_id="fixture",
                        provider_item_id=item_id,
                        review_text=review,
                        review_format="plain",
                        updated_at=NOW,
                    )
                )
                await index_document(conn, SearchKind.REVIEW_TEXT, f"{item_id}:0", review)
            await index_document(conn, SearchKind.WORK_TITLE, str(work_id), title)

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as value,
    ):
        value.headers["Authorization"] = f"Bearer {TOKEN}"
        yield value


async def test_entries_search_unions_titles_and_reviews_across_keyset_pages(
    client: httpx2.AsyncClient,
) -> None:
    first = await client.get("/api/v1/entries", params={"q": "echo", "limit": 1})

    assert first.status_code == 200
    first_page = first.json()
    assert [item["work"]["title"] for item in first_page["items"]] == ["Echo title"]
    assert first_page["next_cursor"]

    second = await client.get(
        "/api/v1/entries",
        params={"q": "echo", "limit": 1, "cursor": first_page["next_cursor"]},
    )

    assert second.status_code == 200
    second_page = second.json()
    assert [item["work"]["title"] for item in second_page["items"]] == ["Unrelated title"]
    assert second_page["next_cursor"] is None
