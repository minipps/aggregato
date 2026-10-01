"""A manual work link remains authoritative when the provider resyncs ."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.api.routes.identity import _assert_undo_safe, _restore
from aggregato.db.schema import (
    creator_external_ids,
    creators,
    merge_log,
    metadata,
    provider_items,
    work_credits,
    works,
)
from aggregato.db.search import sync_create_search_index
from aggregato.domain.enums import Confidence, CreatorKind, MediaFamily, MediaType, Role
from aggregato.domain.models import (
    NormalizedBatch,
    NormalizedCreatorId,
    NormalizedCredit,
    NormalizedWork,
    RawRecord,
)
from aggregato.ingest.merge import merge_creators
from aggregato.ingest.resolve_creator import resolve_creators
from aggregato.ingest.split import split_creator
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


async def test_creator_split_survives_resync_and_merge_undo(conn: SyncConnectionAdapter) -> None:
    async_conn = cast(AsyncConnection, conn)
    context = WriteContext(provider_id="fixture", sync_run_id=1, schema_version=1, now=NOW)

    def artist_batch(title: str) -> NormalizedBatch:
        return NormalizedBatch(
            work=NormalizedWork(media_type=MediaType.TRACK, title=title),
            credits=[
                NormalizedCredit(
                    creator_name="Shared Artist",
                    creator_kind=CreatorKind.PERSON,
                    role=Role.PERFORMER,
                    role_raw="artists",
                    position=0,
                )
            ],
            creator_external_ids=[
                NormalizedCreatorId(
                    creator_name="Shared Artist",
                    namespace="mbid_artist",
                    value="artist-1",
                    confidence=Confidence.ASSERTED,
                )
            ],
        )

    raw_one = RawRecord(native_id="split-one", payload={"id": "split-one"})
    raw_two = RawRecord(native_id="split-two", payload={"id": "split-two"})
    batch_one, batch_two = artist_batch("First Track"), artist_batch("Second Track")
    records = [(raw_one, batch_one), (raw_two, batch_two)]
    assert (await write_batches(async_conn, context, records)).failed == 0

    item_ids = {
        row.native_id: row.work_id
        for row in await conn.execute(select(provider_items.c.native_id, provider_items.c.work_id))
    }
    first_credit = (
        await conn.execute(
            select(work_credits).where(work_credits.c.work_id == item_ids["split-one"])
        )
    ).one()
    source_creator = first_credit.creator_id
    assert (
        await conn.execute(
            select(work_credits.c.creator_id).where(work_credits.c.work_id == item_ids["split-two"])
        )
    ).scalar_one() == source_creator

    split = await split_creator(
        async_conn,
        creator_id=source_creator,
        credit_ids=[first_credit.id],
        new_name="Corrected Artist",
        now=NOW,
    )
    corrected_creator = split.winner_id
    assert (await write_batches(async_conn, context, records)).failed == 0

    credit_rows = list(await conn.execute(select(work_credits)))
    assert len(credit_rows) == 2
    rows = {row.work_id: row for row in credit_rows}
    assert rows[item_ids["split-one"]].creator_id == corrected_creator
    assert rows[item_ids["split-one"]].manual_from_creator_id == source_creator
    assert rows[item_ids["split-one"]].link_confidence == "manual"
    assert rows[item_ids["split-two"]].creator_id == source_creator
    assert (
        await conn.execute(select(creator_external_ids.c.creator_id))
    ).scalar_one() == source_creator
    changed_role = batch_one.model_copy(
        update={"credits": [batch_one.credits[0].model_copy(update={"role_raw": "composer"})]}
    )
    unresolved = await resolve_creators(
        async_conn,
        changed_role,
        MediaFamily.AUDIO,
        source="fixture",
        now=NOW,
        work_id=item_ids["split-one"],
    )
    assert unresolved[0].creator_id == source_creator

    merge_target = uuid.uuid4()
    await conn.execute(
        creators.insert().values(
            id=merge_target,
            kind="person",
            name="Merged Source",
            sort_name="merged source",
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    creator_merge = await merge_creators(
        async_conn, winner_id=merge_target, loser_ids=[source_creator], now=NOW
    )
    assert (await write_batches(async_conn, context, records)).failed == 0
    credit_rows = list(await conn.execute(select(work_credits)))
    assert len(credit_rows) == 2
    rows = {row.work_id: row for row in credit_rows}
    assert rows[item_ids["split-one"]].creator_id == corrected_creator
    assert rows[item_ids["split-one"]].manual_from_creator_id == source_creator
    assert rows[item_ids["split-two"]].creator_id == merge_target

    await _assert_undo_safe(async_conn, creator_merge)
    await _restore(async_conn, creator_merge)
    await conn.execute(
        update(merge_log).where(merge_log.c.id == creator_merge.id).values(undone_at=NOW)
    )
    assert (await write_batches(async_conn, context, records)).failed == 0
    credit_rows = list(await conn.execute(select(work_credits)))
    assert len(credit_rows) == 2
    rows = {row.work_id: row for row in credit_rows}
    assert rows[item_ids["split-one"]].creator_id == corrected_creator
    assert rows[item_ids["split-one"]].manual_from_creator_id == source_creator
    assert rows[item_ids["split-two"]].creator_id == source_creator

    corrected_merge_target = uuid.uuid4()
    await conn.execute(
        creators.insert().values(
            id=corrected_merge_target,
            kind="person",
            name="Corrected Merge Target",
            sort_name="corrected merge target",
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    corrected_merge = await merge_creators(
        async_conn,
        winner_id=corrected_merge_target,
        loser_ids=[corrected_creator],
        now=NOW,
    )
    assert (await write_batches(async_conn, context, records)).failed == 0
    corrected_credit = (
        await conn.execute(
            select(work_credits).where(work_credits.c.work_id == item_ids["split-one"])
        )
    ).one()
    assert corrected_credit.creator_id == corrected_merge_target
    assert corrected_credit.manual_from_creator_id == source_creator

    await _assert_undo_safe(async_conn, corrected_merge)
    await _restore(async_conn, corrected_merge)
    await conn.execute(
        update(merge_log).where(merge_log.c.id == corrected_merge.id).values(undone_at=NOW)
    )
    assert (await write_batches(async_conn, context, records)).failed == 0
    corrected_credit = (
        await conn.execute(
            select(work_credits).where(work_credits.c.work_id == item_ids["split-one"])
        )
    ).one()
    assert corrected_credit.creator_id == corrected_creator
    assert corrected_credit.manual_from_creator_id == source_creator

    await _assert_undo_safe(async_conn, split)
    await _restore(async_conn, split)
    await conn.execute(update(merge_log).where(merge_log.c.id == split.id).values(undone_at=NOW))
    assert (await write_batches(async_conn, context, records)).failed == 0
    credit_rows = list(await conn.execute(select(work_credits)))
    assert len(credit_rows) == 2
    rows = {row.work_id: row for row in credit_rows}
    assert {row.creator_id for row in rows.values()} == {source_creator}
