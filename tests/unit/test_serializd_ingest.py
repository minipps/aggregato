"""Serializd state changes pass through the real host writer without losing history."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.engine import create_engine
from aggregato.db.schema import (
    entries,
    metadata,
    opinions,
    provider_items,
    sync_runs,
    works,
)
from aggregato.db.search import create_search_index
from aggregato.domain.models import RawRecord
from aggregato.ingest.writer import WriteContext, ensure_rating_scales, write_batches
from aggregato.providers.serializd import RATING_SCALE, provider

NOW = datetime(2026, 2, 15, 12, 0, tzinfo=UTC)


def _fts5_available() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()
    return True


pytestmark = pytest.mark.skipif(
    not _fts5_available(), reason="the writer maintains an FTS5 search index"
)


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'serializd-ingest.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await create_search_index(connection)
            await ensure_rating_scales(connection, [RATING_SCALE])
            await connection.execute(
                sync_runs.insert().values(
                    id=1,
                    provider_id="serializd",
                    lineage_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                    attempt=1,
                    mode="incremental",
                    status="running",
                    started_at=NOW,
                )
            )
            yield connection
    finally:
        await engine.dispose()


def _write_context(now: datetime = NOW) -> WriteContext:
    return WriteContext(
        provider_id="serializd",
        sync_run_id=1,
        schema_version=provider.schema_version,
        now=now,
        rating_scales={RATING_SCALE.id: RATING_SCALE},
    )


def _row(
    diary_id: str,
    *,
    is_log: bool = True,
    backdate: str | None = "2025-02-14",
    rating: int | None = None,
    review: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": diary_id,
        "showId": "66573",
        "showName": "The Good Place",
        "showPremiereDate": "2016-09-19",
        "showSeasons": [],
        "seasonId": None,
        "isLog": is_log,
        "isRewatch": False,
        "rating": rating,
        "reviewText": review,
        "like": None,
        "containsSpoiler": None,
    }
    if backdate is not None:
        row["backdate"] = backdate
    return row


async def _count(conn: AsyncConnection, table: Any) -> int:
    result = await conn.execute(select(func.count()).select_from(table))
    return int(result.scalar_one())


async def test_rating_only_update_retracts_watch_but_keeps_its_opinion(
    conn: AsyncConnection,
) -> None:
    diary_id = "serializd-diary-1"
    logged = _row(
        diary_id,
        rating=8,
        review="The first logged version.",
    )
    raw_logged = RawRecord(native_id=diary_id, payload=logged)
    first = await write_batches(
        conn,
        _write_context(),
        [(raw_logged, provider.normalize(raw_logged))],
    )
    assert first.entries_written == 1

    # The composer only exposes a watched-on date when Add to Diary is enabled. The changed
    # rating-only payload intentionally has no backdate.
    rating_only = _row(
        diary_id,
        is_log=False,
        backdate=None,
        rating=9,
        review="The updated rating without a watch.",
    )
    raw_rating_only = RawRecord(native_id=diary_id, payload=rating_only)
    updated = await write_batches(
        conn,
        _write_context(NOW + timedelta(days=1)),
        [(raw_rating_only, provider.normalize(raw_rating_only))],
    )

    assert updated.entries_retracted == 1
    assert await _count(conn, works) == 1
    assert await _count(conn, provider_items) == 1
    assert await _count(conn, entries) == 1

    entry = (await conn.execute(select(entries.c.deleted_at))).one()
    assert entry.deleted_at is not None

    opinion = (
        await conn.execute(
            select(
                opinions.c.rating_raw,
                opinions.c.review_text,
                opinions.c.deleted_at,
            )
        )
    ).one()
    assert Decimal(str(opinion.rating_raw)) == Decimal(9)
    assert opinion.review_text == "The updated rating without a watch."
    assert opinion.deleted_at is None


async def test_distinct_diary_events_for_one_show_remain_separate(
    conn: AsyncConnection,
) -> None:
    first_raw = RawRecord(
        native_id="serializd-diary-2",
        payload=_row("serializd-diary-2", backdate="2025-02-10"),
    )
    second_raw = RawRecord(
        native_id="serializd-diary-3",
        payload=_row("serializd-diary-3", backdate="2025-02-14"),
    )
    records = [(raw, provider.normalize(raw)) for raw in (first_raw, second_raw)]

    counts = await write_batches(conn, _write_context(), records)
    await write_batches(conn, _write_context(), records)

    assert counts.entries_written == 2
    assert await _count(conn, works) == 1
    assert await _count(conn, provider_items) == 2
    rows = (
        await conn.execute(
            select(entries.c.native_id, entries.c.work_id, entries.c.deleted_at).order_by(
                entries.c.native_id
            )
        )
    ).all()
    assert [row.native_id for row in rows] == ["serializd-diary-2", "serializd-diary-3"]
    assert len({row.work_id for row in rows}) == 1
    assert all(row.deleted_at is None for row in rows)
