"""Creator resolution keeps asserted identity global and name matching family-scoped (T067)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from aggregato.db.schema import creator_aliases, creator_external_ids, creators, metadata
from aggregato.domain.enums import AliasKind, Confidence, CreatorKind, MediaFamily, Role
from aggregato.domain.models import (
    NormalizedBatch,
    NormalizedCreatorId,
    NormalizedCredit,
    NormalizedWork,
)
from aggregato.ingest.resolve_creator import resolve_creators
from aggregato.ingest.titles import normalize_title
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> AsyncIterator[SyncConnectionAdapter]:
    engine = create_engine(f"sqlite:///{tmp_path / 'creators.db'}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        yield SyncConnectionAdapter(connection)
    engine.dispose()


def batch(
    name: str = "Alex Example", ids: list[NormalizedCreatorId] | None = None
) -> NormalizedBatch:
    return NormalizedBatch(
        work=NormalizedWork(media_type="film", title="Fixture"),
        credits=[
            NormalizedCredit(
                creator_name=name,
                creator_kind=CreatorKind.PERSON,
                role=Role.DIRECTOR,
                role_raw="Director",
                position=0,
            )
        ],
        creator_external_ids=ids or [],
    )


async def add_creator(conn: SyncConnectionAdapter, name: str, family: MediaFamily) -> uuid.UUID:
    creator_id = uuid.uuid4()
    await conn.execute(
        creators.insert().values(
            id=creator_id,
            kind="person",
            name=name,
            sort_name=normalize_title(name),
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await conn.execute(
        creator_aliases.insert().values(
            creator_id=creator_id,
            name=name,
            normalized=normalize_title(name),
            media_family=str(family),
            kind=str(AliasKind.PRIMARY),
            source="seed",
        )
    )
    return creator_id


async def test_asserted_identifier_matches_across_families(conn: SyncConnectionAdapter) -> None:
    existing = await add_creator(conn, "Alex Example", MediaFamily.PRINT)
    await conn.execute(
        creator_external_ids.insert().values(
            creator_id=existing,
            namespace="anilist",
            value="7",
            source="seed",
            confidence="asserted",
        )
    )
    result = await resolve_creators(
        conn,
        batch(
            ids=[
                NormalizedCreatorId(
                    creator_name="Alex Example",
                    namespace="anilist",
                    value="7",
                    confidence=Confidence.ASSERTED,
                )
            ]
        ),
        MediaFamily.SCREEN,
        source="test",
        now=NOW,
    )
    assert result[0].creator_id == existing
    assert result[0].confidence is Confidence.ASSERTED


async def test_name_match_stays_in_its_media_family(conn: SyncConnectionAdapter) -> None:
    print_creator = await add_creator(conn, "Alex Example", MediaFamily.PRINT)
    result = await resolve_creators(conn, batch(), MediaFamily.SCREEN, source="test", now=NOW)
    assert result[0].creator_id != print_creator


async def test_unique_name_match_is_marked_matched(conn: SyncConnectionAdapter) -> None:
    existing = await add_creator(conn, "Alex Example", MediaFamily.SCREEN)
    result = await resolve_creators(conn, batch(), MediaFamily.SCREEN, source="test", now=NOW)
    assert result[0].creator_id == existing
    assert result[0].confidence is Confidence.MATCHED
