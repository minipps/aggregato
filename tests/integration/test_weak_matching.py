"""Letterboxd's weakest, title-and-year-only identity path remains safe (T111)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

from aggregato.db.schema import metadata, works
from aggregato.db.search import sync_create_search_index
from aggregato.domain.models import RawRecord
from aggregato.ingest.titles import normalize_title
from aggregato.ingest.writer import WriteContext, write_batches
from aggregato.providers.letterboxd import LetterboxdProvider, _items
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[SyncConnectionAdapter]:
    engine = create_engine(f"sqlite:///{tmp_path / 'weak-match.db'}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        sync_create_search_index(connection)
        yield SyncConnectionAdapter(connection)
    engine.dispose()


async def test_letterboxd_title_and_year_link_to_one_existing_work(
    conn: SyncConnectionAdapter,
) -> None:
    payload = await asyncio.to_thread(Path("tests/fixtures/letterboxd/activity.rss").read_bytes)
    item = _items(payload)[0]
    batch = LetterboxdProvider().normalize(RawRecord(native_id=item["guid"], payload=item))
    work_id = uuid.uuid4()
    await conn.execute(
        works.insert().values(
            id=work_id,
            media_type=str(batch.work.media_type),
            title=batch.work.title,
            sort_title=normalize_title(batch.work.title),
            release_year=batch.work.release_year,
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await write_batches(
        conn,
        WriteContext(provider_id="letterboxd", sync_run_id=1, schema_version=1, now=NOW),
        [(RawRecord(native_id=item["guid"], payload=item), batch)],
    )
    count = await conn.execute(select(func.count()).select_from(works))
    assert count.scalar_one() == 1
