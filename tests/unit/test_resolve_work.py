"""The asserted → unique title/year → create work-resolution branches (T066)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from aggregato.db.schema import external_ids, metadata, works
from aggregato.domain.enums import Confidence, MediaType
from aggregato.domain.models import NormalizedBatch, NormalizedExternalId, NormalizedWork
from aggregato.ingest.resolve_work import resolve_work
from aggregato.ingest.titles import normalize_title
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> AsyncIterator[SyncConnectionAdapter]:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        yield SyncConnectionAdapter(connection)
    engine.dispose()


def batch(
    title: str = "The Example",
    year: int | None = 2020,
    ids: list[NormalizedExternalId] | None = None,
) -> NormalizedBatch:
    return NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title=title, release_year=year),
        external_ids=ids or [],
    )


async def add_work(conn: SyncConnectionAdapter, title: str, year: int = 2020) -> uuid.UUID:
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


async def test_asserted_identifier_wins_over_title(conn: SyncConnectionAdapter) -> None:
    existing = await add_work(conn, "Different title")
    await conn.execute(
        external_ids.insert().values(
            work_id=existing,
            namespace="imdb",
            value="tt1",
            source="one",
            confidence="asserted",
            created_at=NOW,
        )
    )
    result = await resolve_work(
        conn,
        batch(
            "The Example",
            ids=[
                NormalizedExternalId(namespace="imdb", value="tt1", confidence=Confidence.ASSERTED)
            ],
        ),
        now=NOW,
    )
    assert result.work_id == existing
    assert result.confidence is Confidence.ASSERTED


async def test_unique_title_and_year_match_is_linked(conn: SyncConnectionAdapter) -> None:
    existing = await add_work(conn, "The Café")
    result = await resolve_work(conn, batch("Cafe"), now=NOW)
    assert result.work_id == existing
    assert result.confidence is Confidence.MATCHED


async def test_no_match_creates_a_new_work(conn: SyncConnectionAdapter) -> None:
    result = await resolve_work(conn, batch(), now=NOW)
    assert result.ambiguous_candidates == ()


async def test_ambiguous_title_and_year_creates_instead_of_merging(
    conn: SyncConnectionAdapter,
) -> None:
    first = await add_work(conn, "The Example")
    second = await add_work(conn, "Example")
    result = await resolve_work(conn, batch(), now=NOW)
    assert result.work_id not in {first, second}
    assert set(result.ambiguous_candidates) == {first, second}
