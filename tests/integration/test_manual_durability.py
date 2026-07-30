"""A manual work link remains authoritative when the provider resyncs (T100, FR-014)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import metadata, provider_items, works
from aggregato.db.search import sync_create_search_index
from aggregato.domain.enums import MediaType
from aggregato.domain.models import NormalizedBatch, NormalizedWork, RawRecord
from aggregato.ingest.titles import normalize_title
from aggregato.ingest.writer import WriteContext, write_batches
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[SyncConnectionAdapter]:
    engine = create_engine(f"sqlite:///{tmp_path / 'manual.db'}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        sync_create_search_index(connection)
        yield SyncConnectionAdapter(connection)
    engine.dispose()


def batch(title: str) -> NormalizedBatch:
    return NormalizedBatch(work=NormalizedWork(media_type=MediaType.FILM, title=title))


async def test_manual_provider_item_link_survives_resync(conn: SyncConnectionAdapter) -> None:
    target = uuid.uuid4()
    await conn.execute(
        works.insert().values(
            id=target,
            media_type="film",
            title="Correct Work",
            sort_title=normalize_title("Correct Work"),
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    context = WriteContext(provider_id="fixture", sync_run_id=1, schema_version=1, now=NOW)
    record = RawRecord(native_id="manual-link", payload={"id": "manual-link"})
    async_conn = cast(AsyncConnection, conn)
    await write_batches(async_conn, context, [(record, batch("Wrong automatic title"))])
    await conn.execute(
        update(provider_items)
        .where(
            provider_items.c.provider_id == "fixture", provider_items.c.native_id == "manual-link"
        )
        .values(work_id=target)
    )

    # A changed title would normally resolve/create differently. The persisted manual link wins.
    await write_batches(async_conn, context, [(record, batch("Changed again"))])
    linked = (await conn.execute(select(provider_items.c.work_id))).scalar_one()
    assert linked == target
