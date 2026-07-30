"""The US2 identity journey through the real writer (T065)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

from aggregato.db.schema import metadata, resolution_queue, works
from aggregato.db.search import sync_create_search_index
from aggregato.domain.enums import Confidence, MediaType
from aggregato.domain.models import (
    NormalizedBatch,
    NormalizedExternalId,
    NormalizedWork,
    RawRecord,
)
from aggregato.ingest.titles import normalize_title
from aggregato.ingest.writer import WriteContext, write_batches
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[SyncConnectionAdapter]:
    engine = create_engine(f"sqlite:///{tmp_path / 'us2.db'}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        sync_create_search_index(connection)
        yield SyncConnectionAdapter(connection)
    engine.dispose()


def batch(
    title: str,
    year: int,
    identifier: str | None = None,
) -> NormalizedBatch:
    return NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title=title, release_year=year),
        external_ids=(
            [
                NormalizedExternalId(
                    namespace="imdb", value=identifier, confidence=Confidence.ASSERTED
                )
            ]
            if identifier
            else []
        ),
    )


async def write(
    conn: SyncConnectionAdapter,
    provider: str,
    native_id: str,
    item: NormalizedBatch,
) -> None:
    await write_batches(
        conn,
        WriteContext(provider_id=provider, sync_run_id=1, schema_version=1, now=NOW),
        [(RawRecord(native_id=native_id, payload={"id": native_id}), item)],
    )


async def add_existing_work(conn: SyncConnectionAdapter, title: str, year: int) -> uuid.UUID:
    work_id = uuid.uuid4()
    await conn.execute(
        works.insert().values(
            id=work_id,
            media_type="film",
            title=title,
            sort_title=normalize_title(title),
            release_year=year,
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return work_id


async def test_two_platforms_unify_only_when_identity_is_safe(conn: SyncConnectionAdapter) -> None:
    """Identifiers win; unique title/year links; collisions queue; different years stay separate."""
    await write(conn, "alpha", "shared-a", batch("Shared Work", 2020, "tt-shared"))
    await write(conn, "beta", "shared-b", batch("A Different Title", 2020, "tt-shared"))

    await write(conn, "alpha", "title-a", batch("The Café", 2021))
    await write(conn, "beta", "title-b", batch("Cafe", 2021))

    first = await add_existing_work(conn, "Collision", 2022)
    second = await add_existing_work(conn, "The Collision", 2022)
    await write(conn, "beta", "collision", batch("Collision", 2022))

    await write(conn, "alpha", "year-a", batch("Same Name", 2018))
    await write(conn, "beta", "year-b", batch("Same Name", 2019))

    grouped = list(
        await conn.execute(
            select(works.c.sort_title, works.c.release_year, func.count().label("n"))
            .group_by(works.c.sort_title, works.c.release_year)
            .order_by(works.c.sort_title, works.c.release_year)
        )
    )
    counts = {(row.sort_title, row.release_year): row.n for row in grouped}
    assert counts[("shared work", 2020)] == 1
    assert counts[("cafe", 2021)] == 1
    # A collision creates a new work rather than silently merging either candidate.
    assert counts[("collision", 2022)] == 3
    assert {counts[("same name", 2018)], counts[("same name", 2019)]} == {1}

    queued = (await conn.execute(select(resolution_queue))).one()
    assert queued.subject == "work"
    assert queued.suggestion_kind == "ambiguous_title_year"
    assert {candidate["id"] for candidate in queued.candidates} == {str(first), str(second)}
