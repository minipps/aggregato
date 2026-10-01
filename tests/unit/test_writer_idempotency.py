"""The writer is idempotent .

"A resync writes nothing new" is the claim, and it has two halves that fail differently:

* Rows the platform identifies rely on a unique constraint plus ``ON CONFLICT``. Easy to get right,
  easy to verify.
* Entries the platform gives **no event id** have no key to conflict on. If the writer does not
  deduplicate those itself, every resync of a feed without event ids duplicates the whole history —
  silently, and worse each time. That is the case most of this file is about.

Everything runs against a real SQLite database with the real schema and the real search index, so
"the index is written in the same transaction" is exercised rather than asserted.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

import aggregato.ingest.writer as writer_module
from aggregato.db.engine import create_engine
from aggregato.db.schema import (
    creator_aliases,
    creators,
    entries,
    external_ids,
    metadata,
    opinions,
    provider_items,
    rating_scales,
    sync_runs,
    work_credits,
    works,
)
from aggregato.db.search import (
    SearchKind,
    create_search_index,
    matching_ref_ids,
)
from aggregato.domain.enums import (
    Confidence,
    CreatorKind,
    EntryKind,
    LoggedPrecision,
    MediaType,
    ReviewFormat,
    Role,
    ScaleKind,
)
from aggregato.domain.models import (
    NormalizedBatch,
    NormalizedCredit,
    NormalizedEntry,
    NormalizedExternalId,
    NormalizedOpinion,
    NormalizedWork,
    RawRecord,
)
from aggregato.domain.ratings import RatingScale
from aggregato.domain.subject_ref import SubjectRef
from aggregato.ingest.writer import (
    WriteContext,
    ensure_rating_scales,
    sort_title_for,
    write_batches,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LOGGED = datetime(2026, 2, 14, 20, 30, tzinfo=UTC)

STARS_5 = RatingScale(
    id="test-stars-5",
    kind=ScaleKind.LINEAR,
    min_value=Decimal("0.5"),
    max_value=Decimal(5),
    step=Decimal("0.5"),
)


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
    not _fts5_available(), reason="this SQLite build has no FTS5; the writer maintains that index"
)


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'writer.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await create_search_index(connection)
            await ensure_rating_scales(connection, [STARS_5])
            # ingest_failures.sync_run_id is a real foreign key, and the writer is only ever
            # called inside a run — so the fixture provides one rather than disabling the key.
            await connection.execute(
                sync_runs.insert().values(
                    id=1,
                    provider_id="test",
                    lineage_id=uuid.uuid4(),
                    attempt=1,
                    mode="incremental",
                    status="running",
                    started_at=NOW,
                )
            )
            yield connection
    finally:
        await engine.dispose()


def ctx(now: datetime = NOW, *, schema_version: int = 1) -> WriteContext:
    return WriteContext(
        provider_id="test",
        sync_run_id=1,
        schema_version=schema_version,
        now=now,
        rating_scales={STARS_5.id: STARS_5},
    )


def batch(
    *,
    title: str = "The Sound of Rain",
    native_id: str | None = "evt-1",
    subject_ref: SubjectRef | None = None,
    rating: Decimal | None = None,
    review: str | None = None,
    ids: list[tuple[str, str]] | None = None,
    logged_at: datetime = LOGGED,
    kind: EntryKind = EntryKind.WATCH,
) -> NormalizedBatch:
    return NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title=title, release_year=2026),
        entries=[
            NormalizedEntry(
                kind=kind,
                logged_at=logged_at,
                logged_precision=LoggedPrecision.EXACT,
                native_id=native_id,
                subject_ref=subject_ref,
            )
        ],
        opinions=(
            [
                NormalizedOpinion(
                    rating_raw=rating,
                    rating_scale_id=STARS_5.id if rating is not None else None,
                    review_text=review,
                    review_format=ReviewFormat.PLAIN if review else None,
                )
            ]
            if rating is not None or review is not None
            else []
        ),
        external_ids=[
            NormalizedExternalId(namespace=ns, value=v, confidence=Confidence.ASSERTED)
            for ns, v in (ids or [("tmdb", "12345")])
        ],
    )


async def _count(conn: AsyncConnection, table: Any) -> int:
    result = await conn.execute(select(func.count()).select_from(table))
    return int(result.scalar_one())


# --- Identified events ------------------------------------------------------------------------


async def test_a_first_write_creates_everything(conn: AsyncConnection) -> None:
    counts = await write_batches(
        conn, ctx(), [(RawRecord(native_id="i-1", payload={"a": 1}), batch())]
    )

    assert counts.seen == 1
    assert counts.written == 1
    assert counts.failed == 0
    assert await _count(conn, works) == 1
    assert await _count(conn, entries) == 1
    assert await _count(conn, provider_items) == 1


async def test_resyncing_the_same_record_writes_nothing_new(conn: AsyncConnection) -> None:
    """, stated as plainly as it can be."""
    record = (RawRecord(native_id="i-1", payload={"a": 1}), batch())

    await write_batches(conn, ctx(), [record])
    before = (await _count(conn, works), await _count(conn, entries), await _count(conn, opinions))

    # A later run, a later clock, identical data.
    await write_batches(conn, ctx(NOW + timedelta(days=1)), [record])
    after = (await _count(conn, works), await _count(conn, entries), await _count(conn, opinions))

    assert before == after == (1, 1, 0)


async def test_resyncing_ten_times_still_writes_nothing_new(conn: AsyncConnection) -> None:
    record = (RawRecord(native_id="i-1", payload={"a": 1}), batch(rating=Decimal("4.5")))
    for day in range(10):
        await write_batches(conn, ctx(NOW + timedelta(days=day)), [record])

    assert await _count(conn, entries) == 1
    assert await _count(conn, opinions) == 1
    assert await _count(conn, provider_items) == 1


async def test_first_seen_at_survives_a_resync(conn: AsyncConnection) -> None:
    """Overwriting it would quietly erase when the archive first learned of the item."""
    record = (RawRecord(native_id="i-1", payload={"a": 1}), batch())
    await write_batches(conn, ctx(), [record])
    await write_batches(conn, ctx(NOW + timedelta(days=30)), [record])

    row = (
        await conn.execute(select(provider_items.c.first_seen_at, provider_items.c.last_seen_at))
    ).one()
    assert row.first_seen_at.replace(tzinfo=UTC) == NOW
    assert row.last_seen_at.replace(tzinfo=UTC) == NOW + timedelta(days=30)


async def test_a_changed_rating_updates_rather_than_duplicating(conn: AsyncConnection) -> None:
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(conn, ctx(), [(raw, batch(rating=Decimal("3.0")))])
    await write_batches(conn, ctx(), [(raw, batch(rating=Decimal("4.5")))])

    assert await _count(conn, opinions) == 1
    row = (await conn.execute(select(opinions.c.rating_raw, opinions.c.rating_normalized))).one()
    assert Decimal(str(row.rating_raw)) == Decimal("4.5")
    # (4.5 - 0.5) / (5 - 0.5) * 100 = 88.89 -> 89
    assert row.rating_normalized == 89


async def test_replay_rejection_preserves_existing_facts_and_schema_version(
    conn: AsyncConnection,
) -> None:
    raw = RawRecord(native_id="replay-item", payload={"id": "replay-item"})
    old = batch(rating=Decimal("4.5"), review="distinctive replay review")
    await write_batches(conn, ctx(), [(raw, old)])
    old_opinion = (await conn.execute(select(opinions.c.id))).scalar_one()
    item_id = (await conn.execute(select(provider_items.c.id))).scalar_one()

    invalid = NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title="The Sound of Rain"),
        opinions=[NormalizedOpinion(rating_raw=Decimal("4"), rating_scale_id="undeclared-scale")],
    )
    counts = await write_batches(
        conn, ctx(schema_version=2), [(raw, invalid)], replace_existing=True
    )

    assert counts.failed == 1
    assert counts.written == 0
    assert (await conn.execute(select(entries.c.deleted_at))).scalar_one() is None
    opinion = (
        await conn.execute(select(opinions.c.deleted_at).where(opinions.c.id == old_opinion))
    ).scalar_one()
    assert opinion is None
    assert (await conn.execute(select(provider_items.c.schema_version))).scalar_one() == 1
    assert await matching_ref_ids(conn, SearchKind.REVIEW_TEXT, "distinctive") == [f"{item_id}:0"]


async def test_replay_write_failure_rolls_back_retirement(
    conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = RawRecord(native_id="replay-item", payload={"id": "replay-item"})
    old = batch(rating=Decimal("4.5"), review="distinctive replay review")
    await write_batches(conn, ctx(), [(raw, old)])
    old_opinion = (await conn.execute(select(opinions.c.id))).scalar_one()
    item_id = (await conn.execute(select(provider_items.c.id))).scalar_one()

    async def fail_after_retirement(*_args: Any, **_kwargs: Any) -> int:
        raise SQLAlchemyError("injected provider-item upsert failure")

    monkeypatch.setattr(writer_module, "_upsert_provider_item", fail_after_retirement)
    empty = NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title="The Sound of Rain")
    )
    counts = await write_batches(conn, ctx(schema_version=2), [(raw, empty)], replace_existing=True)

    assert counts.failed == 1
    assert (await conn.execute(select(entries.c.deleted_at))).scalar_one() is None
    opinion = (
        await conn.execute(select(opinions.c.deleted_at).where(opinions.c.id == old_opinion))
    ).scalar_one()
    assert opinion is None
    assert (await conn.execute(select(provider_items.c.schema_version))).scalar_one() == 1
    assert await matching_ref_ids(conn, SearchKind.REVIEW_TEXT, "distinctive") == [f"{item_id}:0"]


async def test_successful_replay_retires_only_removed_derivatives(conn: AsyncConnection) -> None:
    raw = RawRecord(native_id="replay-item", payload={"id": "replay-item"})
    old = batch(rating=Decimal("4.5"), review="distinctive replay review")
    await write_batches(conn, ctx(), [(raw, old)])
    old_opinion = (await conn.execute(select(opinions.c.id))).scalar_one()

    empty = NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title="The Sound of Rain")
    )
    counts = await write_batches(conn, ctx(schema_version=2), [(raw, empty)], replace_existing=True)

    assert counts.written == 1
    assert (await conn.execute(select(entries.c.deleted_at))).scalar_one() is not None
    opinion = (
        await conn.execute(select(opinions.c.deleted_at).where(opinions.c.id == old_opinion))
    ).scalar_one()
    assert opinion is not None
    assert (await conn.execute(select(provider_items.c.schema_version))).scalar_one() == 2
    assert await matching_ref_ids(conn, SearchKind.REVIEW_TEXT, "distinctive") == []


async def test_a_rating_scale_definition_cannot_change_in_place(conn: AsyncConnection) -> None:
    """A reused scale id with new bounds would make every stored normalized value ambiguous."""
    conflicting = STARS_5.model_copy(update={"max_value": Decimal("10")})

    with pytest.raises(ValueError, match="immutable"):
        await ensure_rating_scales(conn, [conflicting])

    row = (
        await conn.execute(
            select(rating_scales.c.max_value).where(rating_scales.c.id == STARS_5.id)
        )
    ).scalar_one()
    assert Decimal(str(row)) == Decimal("5")


# --- Events with NO native id: the fallback ----------------------------------------------------


async def test_an_entry_without_a_native_id_is_written_once(conn: AsyncConnection) -> None:
    record = (RawRecord(native_id="i-1", payload={"a": 1}), batch(native_id=None))
    await write_batches(conn, ctx(), [record])
    assert await _count(conn, entries) == 1


async def test_resyncing_an_entry_without_a_native_id_does_not_duplicate(
    conn: AsyncConnection,
) -> None:
    """The load-bearing case.

    There is no unique key to conflict on, so this is entirely the writer's own deduplication on
    ``(provider_item_id, kind, logged_at, subject_ref)``. Without it a feed with no event ids
    duplicates its whole history on every single resync — the exact failure  forbids.
    """
    record = (RawRecord(native_id="i-1", payload={"a": 1}), batch(native_id=None))

    for day in range(5):
        await write_batches(conn, ctx(NOW + timedelta(days=day)), [record])

    assert await _count(conn, entries) == 1, "a feed without event ids duplicated on resync"


async def test_two_genuinely_different_unidentified_entries_both_land(
    conn: AsyncConnection,
) -> None:
    """Deduplication must not become deletion: a rewatch on another day is a different event."""
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(conn, ctx(), [(raw, batch(native_id=None, logged_at=LOGGED))])
    await write_batches(
        conn, ctx(), [(raw, batch(native_id=None, logged_at=LOGGED + timedelta(days=7)))]
    )

    assert await _count(conn, entries) == 2


async def test_unidentified_entries_differing_only_by_kind_both_land(
    conn: AsyncConnection,
) -> None:
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(conn, ctx(), [(raw, batch(native_id=None, kind=EntryKind.WATCH))])
    await write_batches(conn, ctx(), [(raw, batch(native_id=None, kind=EntryKind.REWATCH))])

    assert await _count(conn, entries) == 2


async def test_unidentified_entries_differing_only_by_subject_ref_both_land(
    conn: AsyncConnection,
) -> None:
    """Episode 1 and episode 2 logged at the same instant are two events, not one."""
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(
        conn, ctx(), [(raw, batch(native_id=None, subject_ref=SubjectRef(season=1, episode=1)))]
    )
    await write_batches(
        conn, ctx(), [(raw, batch(native_id=None, subject_ref=SubjectRef(season=1, episode=2)))]
    )

    assert await _count(conn, entries) == 2


async def test_a_whole_work_entry_and_a_subunit_entry_are_distinct(
    conn: AsyncConnection,
) -> None:
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(conn, ctx(), [(raw, batch(native_id=None, subject_ref=None))])
    await write_batches(
        conn, ctx(), [(raw, batch(native_id=None, subject_ref=SubjectRef(episode=1)))]
    )

    assert await _count(conn, entries) == 2


async def test_the_same_subunit_entry_resynced_does_not_duplicate(conn: AsyncConnection) -> None:
    record = (
        RawRecord(native_id="i-1", payload={"a": 1}),
        batch(native_id=None, subject_ref=SubjectRef(season=2, episode=5)),
    )
    await write_batches(conn, ctx(), [record])
    await write_batches(conn, ctx(NOW + timedelta(days=1)), [record])

    assert await _count(conn, entries) == 1


# --- Identity by asserted identifier ----------------------------------------------------------


async def test_two_records_sharing_an_identifier_land_on_one_work(conn: AsyncConnection) -> None:
    """'s cheap, reliable half: a shared identifier unifies with no guessing."""
    await write_batches(
        conn,
        ctx(),
        [
            (
                RawRecord(native_id="a-1", payload={}),
                batch(title="Solaris", ids=[("imdb", "tt0069293")]),
            )
        ],
    )
    await write_batches(
        conn,
        ctx(),
        [
            (
                RawRecord(native_id="b-1", payload={}),
                batch(title="Солярис", native_id="evt-2", ids=[("imdb", "tt0069293")]),
            )
        ],
    )

    assert await _count(conn, works) == 1
    assert await _count(conn, entries) == 2


async def test_records_with_different_identifiers_stay_separate(conn: AsyncConnection) -> None:
    await write_batches(
        conn, ctx(), [(RawRecord(native_id="a", payload={}), batch(ids=[("imdb", "tt1")]))]
    )
    await write_batches(
        conn,
        ctx(),
        [(RawRecord(native_id="b", payload={}), batch(native_id="evt-2", ids=[("imdb", "tt2")]))],
    )

    assert await _count(conn, works) == 2


async def test_every_identifier_in_the_payload_is_stored(conn: AsyncConnection) -> None:
    """: including ones Aggregato has no use for, because match quality depends on it."""
    await write_batches(
        conn,
        ctx(),
        [
            (
                RawRecord(native_id="a", payload={}),
                batch(ids=[("imdb", "tt1"), ("tmdb", "9"), ("wikidata", "Q42")]),
            )
        ],
    )

    assert await _count(conn, external_ids) == 3


async def test_reasserting_an_identifier_does_not_duplicate_it(conn: AsyncConnection) -> None:
    record = (RawRecord(native_id="a", payload={}), batch(ids=[("imdb", "tt1")]))
    await write_batches(conn, ctx(), [record])
    await write_batches(conn, ctx(), [record])

    assert await _count(conn, external_ids) == 1


# --- Search index, same transaction -----------------------------------------------------------


async def test_a_written_work_is_immediately_searchable(conn: AsyncConnection) -> None:
    """The index is written in the same transaction as the row.

    Nothing is committed here, so if indexing had been deferred to a separate transaction or a
    trigger, this query would find nothing.
    """
    await write_batches(
        conn, ctx(), [(RawRecord(native_id="a", payload={}), batch(title="The Sound of Rain"))]
    )

    ref_ids = await matching_ref_ids(conn, SearchKind.WORK_TITLE, "rain")
    found = await conn.execute(
        select(works.c.title).where(works.c.id.in_([uuid.UUID(r) for r in ref_ids]))
    )
    assert [row.title for row in found] == ["The Sound of Rain"]


async def test_a_work_is_findable_by_its_original_title(conn: AsyncConnection) -> None:
    """Both title forms go into one document, so a translation is findable by the original."""
    batch_with_original = NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title="Solaris", original_title="Solyaris"),
        external_ids=[
            NormalizedExternalId(namespace="imdb", value="tt1", confidence=Confidence.ASSERTED)
        ],
    )
    await write_batches(conn, ctx(), [(RawRecord(native_id="a", payload={}), batch_with_original)])

    ref_ids = await matching_ref_ids(conn, SearchKind.WORK_TITLE, "solyaris")
    found = await conn.execute(
        select(works.c.title).where(works.c.id.in_([uuid.UUID(r) for r in ref_ids]))
    )
    assert [row.title for row in found] == ["Solaris"]


async def test_a_review_is_searchable(conn: AsyncConnection) -> None:
    await write_batches(
        conn,
        ctx(),
        [(RawRecord(native_id="a", payload={}), batch(review="slow and wonderful"))],
    )
    rows = await conn.execute(
        text(
            "SELECT ref_id FROM search_index WHERE kind = :kind AND search_index MATCH :term"
        ).bindparams(kind=str(SearchKind.REVIEW_TEXT), term='"wonderful"')
    )
    assert len(rows.fetchall()) == 1


async def test_a_retitled_work_is_not_findable_under_its_old_title(
    conn: AsyncConnection,
) -> None:
    """Reindexing replaces, so a corrected title does not stay searchable under its mistake."""
    raw = RawRecord(native_id="a", payload={})
    await write_batches(conn, ctx(), [(raw, batch(title="Wrong Title", ids=[("imdb", "tt1")]))])
    await write_batches(conn, ctx(), [(raw, batch(title="Right Title", ids=[("imdb", "tt1")]))])

    assert await matching_ref_ids(conn, SearchKind.WORK_TITLE, "wrong") == []
    assert len(await matching_ref_ids(conn, SearchKind.WORK_TITLE, "right")) == 1


# --- Rejection, not corruption ----------------------------------------------------------------


async def test_a_rating_naming_an_undeclared_scale_becomes_a_failure(
    conn: AsyncConnection,
) -> None:
    """The normalized value would be uncomputable, so this is a rejection rather than a null."""
    bad = NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title="X"),
        opinions=[NormalizedOpinion(rating_raw=Decimal("3"), rating_scale_id="no-such-scale")],
    )
    counts = await write_batches(conn, ctx(), [(RawRecord(native_id="a", payload={"x": 1}), bad)])

    assert counts.failed == 1
    assert counts.written == 0
    assert await _count(conn, opinions) == 0


async def test_one_bad_record_does_not_stop_the_others(conn: AsyncConnection) -> None:
    """: a poisoned record costs its own row, not the run."""
    good = (RawRecord(native_id="g1", payload={}), batch(native_id="e1", ids=[("imdb", "tt1")]))
    bad = (
        RawRecord(native_id="b1", payload={"broken": True}),
        NormalizedBatch(
            work=NormalizedWork(media_type=MediaType.FILM, title="Bad"),
            opinions=[NormalizedOpinion(rating_raw=Decimal("3"), rating_scale_id="nope")],
        ),
    )
    good2 = (RawRecord(native_id="g2", payload={}), batch(native_id="e2", ids=[("imdb", "tt2")]))

    counts = await write_batches(conn, ctx(), [good, bad, good2])

    assert counts.seen == 3
    assert counts.written == 2
    assert counts.failed == 1
    assert await _count(conn, entries) == 2


async def test_a_late_record_failure_rolls_back_every_record_side_effect(
    conn: AsyncConnection,
) -> None:
    """A rating failure after resolution must leave no partial archive graph behind."""
    bad = batch(rating=Decimal("4.25")).model_copy(
        update={
            "credits": [
                NormalizedCredit(
                    creator_name="Rolled Back Creator",
                    creator_kind=CreatorKind.PERSON,
                    role=Role.DIRECTOR,
                    role_raw="Director",
                    position=0,
                )
            ]
        }
    )

    counts = await write_batches(
        conn,
        ctx(),
        [(RawRecord(native_id="bad", payload={"native": "kept"}), bad)],
    )

    assert counts.failed == 1
    assert counts.written == 0
    for table in (
        provider_items,
        works,
        external_ids,
        creators,
        creator_aliases,
        work_credits,
        entries,
        opinions,
    ):
        assert await _count(conn, table) == 0, table.name

    # The failure itself is deliberately outside the record savepoint.
    from aggregato.ingest.failures import unresolved_failures

    assert [failure.raw_payload for failure in await unresolved_failures(conn)] == [
        {"native": "kept"}
    ]


async def test_a_captured_failure_keeps_its_payload(conn: AsyncConnection) -> None:
    """Without the payload, "one record failed" is an unreproducible bug report ."""
    from aggregato.ingest.failures import unresolved_failures

    bad = NormalizedBatch(
        work=NormalizedWork(media_type=MediaType.FILM, title="X"),
        opinions=[NormalizedOpinion(rating_raw=Decimal("3"), rating_scale_id="nope")],
    )
    await write_batches(conn, ctx(), [(RawRecord(native_id="a", payload={"keep": "me"}), bad)])

    captured = await unresolved_failures(conn)
    assert len(captured) == 1
    assert captured[0].raw_payload == {"keep": "me"}


# --- A record that now logs nothing withdraws what it logged before -----------------------------


def _retracted(source: NormalizedBatch) -> NormalizedBatch:
    return source.model_copy(update={"entries": [], "retracts_entries": True})


def _no_entries(source: NormalizedBatch) -> NormalizedBatch:
    """Silent about entries, without claiming a withdrawal — the Goodreads "no Date Read" shape."""
    return source.model_copy(update={"entries": []})


async def test_a_record_that_merely_states_no_entry_deletes_nothing(
    conn: AsyncConnection,
) -> None:
    """The distinction this flag exists for.

    A Goodreads row whose Date Read is absent says nothing about whether the book was read — an
    older CSV re-imported over a newer one is the realistic way to hit it. Inferring a tombstone
    from that silence would destroy a real read, which is why the writer waits to be told.
    """
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(conn, ctx(), [(raw, batch())])

    counts = await write_batches(conn, ctx(NOW + timedelta(days=1)), [(raw, _no_entries(batch()))])

    assert counts.entries_retracted == 0
    assert (await conn.execute(select(entries.c.deleted_at))).scalar_one() is None


def test_a_batch_cannot_log_and_withdraw_at_once() -> None:
    with pytest.raises(ValueError, match="retracts_entries"):
        NormalizedBatch.model_validate(batch().model_dump() | {"retracts_entries": True})


async def test_a_record_that_now_logs_nothing_tombstones_what_it_logged(
    conn: AsyncConnection,
) -> None:
    """AniList's plan-to-watch case: the platform withdrew the event, so the log must too."""
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(conn, ctx(), [(raw, batch())])

    counts = await write_batches(conn, ctx(NOW + timedelta(days=1)), [(raw, _retracted(batch()))])

    assert counts.entries_retracted == 1
    # A tombstone, not a delete: the row and its history survive .
    assert await _count(conn, entries) == 1
    deleted_at = (await conn.execute(select(entries.c.deleted_at))).scalar_one()
    assert deleted_at is not None


async def test_a_status_that_logs_again_revives_the_entry(conn: AsyncConnection) -> None:
    """Reversible in both directions — moving a title back off the backlog must restore its log."""
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(conn, ctx(), [(raw, batch())])
    await write_batches(conn, ctx(NOW + timedelta(days=1)), [(raw, _retracted(batch()))])

    await write_batches(conn, ctx(NOW + timedelta(days=2)), [(raw, batch())])

    assert await _count(conn, entries) == 1
    assert (await conn.execute(select(entries.c.deleted_at))).scalar_one() is None


async def test_a_newer_status_replaces_the_recorded_entry(conn: AsyncConnection) -> None:
    """One list entry is one state: a later kind rewrites the row rather than adding a second."""
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(conn, ctx(), [(raw, batch(kind=EntryKind.WATCH))])

    later = batch(kind=EntryKind.REWATCH, logged_at=LOGGED + timedelta(days=3))
    await write_batches(conn, ctx(NOW + timedelta(days=1)), [(raw, later)])

    row = (await conn.execute(select(entries.c.kind, entries.c.logged_at))).one()
    assert await _count(conn, entries) == 1
    assert row.kind == EntryKind.REWATCH
    assert row.logged_at.replace(tzinfo=UTC) == LOGGED + timedelta(days=3)


async def test_retraction_leaves_another_providers_entry_alone(conn: AsyncConnection) -> None:
    """The withdrawal is scoped to the item that made the claim, not to the work."""
    raw = RawRecord(native_id="i-1", payload={"a": 1})
    await write_batches(conn, ctx(), [(raw, batch())])
    other = WriteContext(
        provider_id="other",
        sync_run_id=1,
        schema_version=1,
        now=NOW,
        rating_scales={STARS_5.id: STARS_5},
    )
    # Same title, so both resolve to one work; a different provider, so a different item.
    await write_batches(conn, other, [(RawRecord(native_id="o-1", payload={"b": 2}), batch())])

    counts = await write_batches(conn, ctx(NOW + timedelta(days=1)), [(raw, _retracted(batch()))])

    assert counts.entries_retracted == 1
    surviving = await conn.execute(
        select(entries.c.provider_id).where(entries.c.deleted_at.is_(None))
    )
    assert [row.provider_id for row in surviving] == ["other"]


# --- Artwork backfill ---------------------------------------------------------------------------


def _with_image(source: NormalizedBatch, url: str | None) -> NormalizedBatch:
    return source.model_copy(update={"work": source.work.model_copy(update={"image_url": url})})


async def _image_url(conn: AsyncConnection) -> str | None:
    return (await conn.execute(select(works.c.image_url))).scalar_one()


async def test_artwork_reaches_a_work_this_run_did_not_create(conn: AsyncConnection) -> None:
    """A work created before the provider read artwork must still get it on the next sync.

    ``image_url`` used to be written only by the insert in ``resolve_work``, so any work that
    resolved to an existing row kept NULL forever and its entries never linked an image.
    """
    plain = (RawRecord(native_id="i-1", payload={"a": 1}), batch())
    await write_batches(conn, ctx(), [plain])
    assert await _image_url(conn) is None

    art = "https://example.invalid/rain.jpg"
    await write_batches(
        conn, ctx(), [(RawRecord(native_id="i-1", payload={"a": 1}), _with_image(batch(), art))]
    )

    assert await _image_url(conn) == art


async def test_artwork_already_stated_is_not_overwritten(conn: AsyncConnection) -> None:
    """An existing URL is some provider's stated payload; a resync must not churn it ."""
    first = "https://example.invalid/first.jpg"
    record = (RawRecord(native_id="i-1", payload={"a": 1}), _with_image(batch(), first))
    await write_batches(conn, ctx(), [record])

    await write_batches(
        conn,
        ctx(),
        [
            (
                RawRecord(native_id="i-2", payload={"a": 2}),
                _with_image(batch(), "https://example.invalid/second.jpg"),
            )
        ],
    )

    assert await _image_url(conn) == first


# --- sort_title ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("The Sound of Rain", "sound of rain"),
        ("A Quiet Place", "quiet place"),
        ("An Education", "education"),
        ("Solaris", "solaris"),
        ("  Padded  ", "padded"),
        ("Theatre of Blood", "theatre of blood"),  # "The" must not be stripped from "Theatre"
    ],
)
def test_sort_title(title: str, expected: str) -> None:
    assert sort_title_for(title) == expected
