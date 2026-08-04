"""snapshots preserve all rows touched by a merge or split ."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from aggregato.api.routes.identity import _restore
from aggregato.db.schema import creators, merge_log, metadata, work_credits, works
from aggregato.db.search import sync_create_search_index
from aggregato.ingest.merge import merge_works
from aggregato.ingest.split import split_creator
from aggregato.ingest.titles import normalize_title
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> AsyncIterator[SyncConnectionAdapter]:
    engine = create_engine(f"sqlite:///{tmp_path / 'merge.db'}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        sync_create_search_index(connection)
        yield SyncConnectionAdapter(connection)
    engine.dispose()


async def add_work(conn: SyncConnectionAdapter, title: str) -> uuid.UUID:
    id = uuid.uuid4()
    await conn.execute(
        works.insert().values(
            id=id,
            media_type="film",
            title=title,
            sort_title=normalize_title(title),
            release_year=2020,
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return id


async def test_work_merge_snapshot_restores_the_original_rows(conn: SyncConnectionAdapter) -> None:
    winner, loser = await add_work(conn, "Winner"), await add_work(conn, "Loser")
    log = await merge_works(conn, winner_id=winner, loser_ids=[loser], now=NOW)
    assert set((await conn.execute(select(works.c.id))).scalars()) == {winner}
    await _restore(conn, log)
    assert set((await conn.execute(select(works.c.id))).scalars()) == {winner, loser}


async def test_creator_split_snapshot_restores_exact_selected_credits(
    conn: SyncConnectionAdapter,
) -> None:
    work = await add_work(conn, "Work")
    creator = uuid.uuid4()
    await conn.execute(
        creators.insert().values(
            id=creator,
            kind="person",
            name="Same Name",
            sort_name="same name",
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await conn.execute(
        work_credits.insert().values(
            work_id=work,
            creator_id=creator,
            role="writer",
            position=0,
            source="fixture",
            link_confidence="matched",
        )
    )
    credit_id = (await conn.execute(select(work_credits.c.id))).scalar_one()
    log = await split_creator(
        conn, creator_id=creator, credit_ids=[credit_id], new_name="Other", now=NOW
    )
    assert (await conn.execute(select(work_credits.c.creator_id))).scalar_one() == log.winner_id
    await _restore(conn, log)
    assert (await conn.execute(select(work_credits.c.creator_id))).scalar_one() == creator
    assert (await conn.execute(select(merge_log.c.id))).scalar_one() == log.id
