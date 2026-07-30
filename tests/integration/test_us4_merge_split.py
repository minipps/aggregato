"""US4 archive journey: merge, split, and undo through the identity services (T098)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.api.routes.identity import _restore
from aggregato.db.schema import creators, entries, metadata, provider_items, work_credits, works
from aggregato.db.search import sync_create_search_index
from aggregato.ingest.merge import merge_works
from aggregato.ingest.split import split_creator
from aggregato.ingest.titles import normalize_title
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[SyncConnectionAdapter]:
    engine = create_engine(f"sqlite:///{tmp_path / 'us4.db'}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        sync_create_search_index(connection)
        yield SyncConnectionAdapter(connection)
    engine.dispose()


async def add_work(conn: SyncConnectionAdapter, title: str) -> uuid.UUID:
    work = uuid.uuid4()
    await conn.execute(
        works.insert().values(
            id=work,
            media_type="film",
            title=title,
            sort_title=normalize_title(title),
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    item = (
        await conn.execute(
            provider_items.insert()
            .values(
                provider_id="fixture",
                native_id=str(work),
                work_id=work,
                title_as_given=title,
                raw_payload={},
                schema_version=1,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            .returning(provider_items.c.id)
        )
    ).scalar_one()
    await conn.execute(
        entries.insert().values(
            work_id=work,
            provider_id="fixture",
            provider_item_id=item,
            native_id=f"entry-{work}",
            kind="finish",
            logged_at=NOW,
            logged_precision="exact",
            ingested_at=NOW,
        )
    )
    return work


async def test_merge_split_and_undo_leave_no_archive_records_lost(
    conn: SyncConnectionAdapter,
) -> None:
    winner, loser = await add_work(conn, "Winner"), await add_work(conn, "Loser")
    async_conn = cast(AsyncConnection, conn)
    merge = await merge_works(async_conn, winner_id=winner, loser_ids=[loser], now=NOW)
    assert set((await conn.execute(select(entries.c.work_id))).scalars()) == {winner}
    await _restore(conn, merge)
    assert set((await conn.execute(select(entries.c.work_id))).scalars()) == {winner, loser}

    creator = uuid.uuid4()
    await conn.execute(
        creators.insert().values(
            id=creator,
            kind="person",
            name="Shared",
            sort_name="shared",
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    for work in (winner, loser):
        await conn.execute(
            work_credits.insert().values(
                work_id=work,
                creator_id=creator,
                role="writer",
                position=0,
                source=str(work),
                link_confidence="matched",
            )
        )
    credit = (
        await conn.execute(select(work_credits.c.id).where(work_credits.c.work_id == loser))
    ).scalar_one()
    split = await split_creator(
        async_conn, creator_id=creator, credit_ids=[credit], new_name="Separate", now=NOW
    )
    assert (
        await conn.execute(select(work_credits.c.creator_id).where(work_credits.c.id == credit))
    ).scalar_one() == split.winner_id
    await _restore(conn, split)
    assert (
        await conn.execute(select(work_credits.c.creator_id).where(work_credits.c.id == credit))
    ).scalar_one() == creator
