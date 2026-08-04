"""Creator resolution keeps asserted identity global and name matching family-scoped ."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import (
    creator_aliases,
    creator_external_ids,
    creators,
    metadata,
    resolution_queue,
)
from aggregato.domain.enums import AliasKind, Confidence, CreatorKind, MediaFamily, Role
from aggregato.domain.models import (
    NormalizedBatch,
    NormalizedCreatorId,
    NormalizedCredit,
    NormalizedWork,
)
from aggregato.ingest.resolve_creator import CreatorResolutionMemo, resolve_creators
from aggregato.ingest.resolve_queue import (
    queue_cross_family_creator_suggestions,
    supersede_stale_open_items,
)
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


async def test_cross_family_name_is_suggested_not_auto_linked(conn: SyncConnectionAdapter) -> None:
    print_creator = await add_creator(conn, "Alex Example", MediaFamily.PRINT)
    result = await resolve_creators(conn, batch(), MediaFamily.SCREEN, source="test", now=NOW)

    await queue_cross_family_creator_suggestions(
        conn,
        provider_id="test",
        provider_item_id=None,
        batch=batch(),
        resolutions=result,
        family=MediaFamily.SCREEN,
        now=NOW,
    )

    row = (await conn.execute(resolution_queue.select())).one()
    assert row.subject == "creator"
    assert row.suggestion_kind == "cross_family_name"
    assert row.proposed["creator_id"] == str(result[0].creator_id)
    assert row.candidates == [
        {
            "id": str(print_creator),
            "reason": "same normalized name in another media family",
            "name": "Alex Example",
            "media_family": "print",
        }
    ]


async def test_unique_name_match_is_marked_matched(conn: SyncConnectionAdapter) -> None:
    existing = await add_creator(conn, "Alex Example", MediaFamily.SCREEN)
    result = await resolve_creators(conn, batch(), MediaFamily.SCREEN, source="test", now=NOW)
    assert result[0].creator_id == existing
    assert result[0].confidence is Confidence.MATCHED


async def test_resolution_memo_reuses_a_repeated_credit_set(conn: SyncConnectionAdapter) -> None:
    memo: CreatorResolutionMemo = {}
    first = await resolve_creators(
        conn, batch(), MediaFamily.SCREEN, source="test", now=NOW, memo=memo
    )
    second = await resolve_creators(
        conn, batch(), MediaFamily.SCREEN, source="test", now=NOW, memo=memo
    )

    assert second == first
    assert len(memo) == 1


async def test_full_refresh_supersedes_only_older_open_items(
    conn: SyncConnectionAdapter,
) -> None:
    """A current full refresh replaces stale suggestions without erasing audit history."""
    refreshed_at = NOW + timedelta(days=1)
    await conn.execute(
        resolution_queue.insert(),
        [
            {
                "id": 1,
                "subject": "creator",
                "provider_id": "fixture",
                "candidates": [],
                "proposed": {},
                "created_at": NOW,
            },
            {
                "id": 2,
                "subject": "creator",
                "provider_id": "fixture",
                "candidates": [],
                "proposed": {},
                "created_at": refreshed_at,
            },
            {
                "id": 3,
                "subject": "creator",
                "provider_id": "other",
                "candidates": [],
                "proposed": {},
                "created_at": NOW,
            },
        ],
    )

    await supersede_stale_open_items(
        cast(AsyncConnection, conn),
        provider_id="fixture",
        refreshed_at=refreshed_at,
        now=refreshed_at,
    )

    rows = {row.id: row for row in await conn.execute(resolution_queue.select())}
    assert rows[1].decision == "ignored"
    assert rows[1].decided_at == refreshed_at.replace(tzinfo=None)
    assert rows[2].decided_at is None
    assert rows[3].decided_at is None
