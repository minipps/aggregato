"""The validated writer — the only thing in the system that writes ingested data .

Everything a provider produces passes through here, and it is validated **in this process**, never
trusted from the child: the child runs plugin code (research.md , ). A record that fails
validation becomes an ``ingest_failure`` with its payload, and the run continues .

Three properties this module exists to guarantee:

**Idempotency .** A resync writes nothing new. For rows the platform identifies, that is a
unique constraint plus ``ON CONFLICT`` — never a pre-``SELECT``, which races. For entries the
platform gives no event id, there is no key to conflict on, so the writer deduplicates on
``(provider_item_id, kind, logged_at, subject_ref)`` itself. That fallback is the interesting half:
without it, every resync of a feed without event ids would duplicate the entire history.

**Search stays in step.** The search index is written in the **same transaction** as the row it
describes (research.md ). A rolled-back write cannot leave a searchable ghost, and there is one
implementation of *when* to index rather than two dialects' worth of trigger DDL.

**Nothing is destroyed.** Deletes are tombstones (``deleted_at``), never row removal, and the
inferred-delete path is guarded three ways over (see :func:`infer_deletes`,  completes it).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import (
    entries,
    external_ids,
    opinions,
    provider_items,
    work_credits,
    works,
)
from aggregato.db.search import SearchKind, index_document, rebuild_work_document
from aggregato.db.upsert import upsert_stmt
from aggregato.domain.enums import Confidence, IngestStage
from aggregato.domain.families import family_of
from aggregato.domain.identifiers import canonicalize_identifier
from aggregato.domain.models import NormalizedBatch, RawRecord
from aggregato.domain.ratings import RatingScale, normalize_rating
from aggregato.domain.subject_ref import SubjectRef, validate_subject_ref
from aggregato.images.cache import register_source_on_connection
from aggregato.ingest.failures import capture_failure
from aggregato.ingest.resolve_creator import CreatorResolutionMemo, resolve_creators
from aggregato.ingest.resolve_queue import (
    queue_cross_family_creator_suggestions,
    queue_work_ambiguity,
)
from aggregato.ingest.resolve_work import WorkResolution, resolve_work
from aggregato.ingest.titles import normalize_title


@dataclass
class WriteCounts:
    """What one run actually did, for the ``sync_runs`` row and the operator's history."""

    seen: int = 0
    written: int = 0
    failed: int = 0
    entries_written: int = 0
    entries_retracted: int = 0
    """Rows tombstoned because the record that produced them now logs nothing (see
    :func:`_retract_entries`). Counted separately from ``entries_written``: an operator reading a
    run's history should see a withdrawal as its own event, not as a write that did not happen."""
    opinions_written: int = 0
    failure_ids: list[int] = field(default_factory=list)


class ValidationRejection(ValueError):
    """A record violated the ingest contract. Becomes an ``ingest_failure``, not a write."""


@dataclass(frozen=True)
class WriteContext:
    """What the writer needs that is not in the batch itself."""

    provider_id: str
    sync_run_id: int
    schema_version: int
    now: datetime
    #: Scales the provider declared, by id. A rating naming a scale outside this set is a provider
    #: bug: the normalized value would be uncomputable and silently null.
    rating_scales: dict[str, RatingScale] = field(default_factory=dict)


async def infer_deletes(
    conn: AsyncConnection,
    *,
    provider_id: str,
    seen_since: datetime,
) -> int:
    """Tombstone stale entries only after dispatch has passed all three safety guards.

    This function deliberately has no configuration or run-status arguments: callers must prove
    those guards before reaching this destructive operation, making it impossible for a normal
    incremental write to accidentally infer deletion.
    """
    stale_items = select(provider_items.c.id).where(
        provider_items.c.provider_id == provider_id,
        provider_items.c.last_seen_at < seen_since,
    )
    result = await conn.execute(
        update(entries)
        .where(entries.c.provider_item_id.in_(stale_items), entries.c.deleted_at.is_(None))
        .values(deleted_at=seen_since)
    )
    await conn.execute(
        update(opinions)
        .where(opinions.c.provider_item_id.in_(stale_items), opinions.c.deleted_at.is_(None))
        .values(deleted_at=seen_since)
    )
    return result.rowcount


async def write_batches(
    conn: AsyncConnection,
    ctx: WriteContext,
    records: Sequence[tuple[RawRecord, NormalizedBatch]],
) -> WriteCounts:
    """Write every batch, capturing the ones that fail without stopping.

    Args:
        conn: Connection inside one transaction. The caller owns commit, so a failed run leaves the
            archive exactly as it was.
        ctx: Provider, run, and clock context.
        records: Pairs of the raw record and what ``normalize`` made of it. The raw record is needed
            because ``provider_items.raw_payload`` stores it as the replay source .

    Returns:
        Counts for the run row.
    """
    counts = WriteCounts()
    creator_memo: CreatorResolutionMemo = {}
    for raw, batch in records:
        counts.seen += 1
        # Resolution creates rows as it goes and caches the result for the rest of the batch. Keep
        # both effects record-local until the savepoint commits: a later rating/index failure must
        # not leave either a rolled-back creator id in the memo or a partial relational graph.
        record_memo = dict(creator_memo)
        record_counts = (
            counts.entries_written,
            counts.entries_retracted,
            counts.opinions_written,
        )
        savepoint = await conn.begin_nested()
        failure_stage = IngestStage.VALIDATE
        failure_error: BaseException | None = None
        try:
            await _write_one(conn, ctx, raw, batch, counts, record_memo)
        except (ValidationRejection, ValueError) as exc:
            failure_error = exc
            await savepoint.rollback()
        except SQLAlchemyError as exc:
            # A database/index failure belongs to this record. Rolling back the nested transaction
            # keeps the caller's batch transaction usable for later records and for the failure row.
            failure_error = exc
            failure_stage = IngestStage.WRITE
            await savepoint.rollback()
        else:
            await savepoint.commit()
            creator_memo.update(record_memo)
            counts.written += 1
            continue

        # One poisoned record must never cost the operator the rest of the run. The savepoint is
        # already rolled back, so this insert survives while all record side effects disappear.
        assert failure_error is not None
        (
            counts.entries_written,
            counts.entries_retracted,
            counts.opinions_written,
        ) = record_counts
        counts.failed += 1
        failure_id = await capture_failure(
            conn,
            provider_id=ctx.provider_id,
            sync_run_id=ctx.sync_run_id,
            stage=failure_stage,
            error=failure_error,
            raw_payload=raw.payload,
            now=ctx.now,
            native_id=raw.native_id,
        )
        counts.failure_ids.append(failure_id)
    return counts


async def _write_one(
    conn: AsyncConnection,
    ctx: WriteContext,
    raw: RawRecord,
    batch: NormalizedBatch,
    counts: WriteCounts,
    creator_memo: CreatorResolutionMemo,
) -> None:
    """Write one normalized batch. Raises rather than half-writing."""
    batch = _canonicalize_batch_identifiers(batch)
    _validate(ctx, batch)

    # A manual queue decision changes the provider item's durable work link.  Honor it before
    # reevaluating automatic evidence on resync: otherwise a cautious operator correction would be
    # overwritten by the exact heuristic it was made to correct .
    existing_work_id = (
        await conn.execute(
            select(provider_items.c.work_id).where(
                provider_items.c.provider_id == ctx.provider_id,
                provider_items.c.native_id == raw.native_id,
            )
        )
    ).scalar_one_or_none()
    resolution = (
        WorkResolution(existing_work_id, Confidence.MANUAL)
        if existing_work_id is not None
        else await resolve_work(conn, batch, now=ctx.now)
    )
    work_id = resolution.work_id
    if existing_work_id is not None:
        # A provider correcting its own existing item may update the canonical title. A new
        # provider's assertion must remain a provider-item title, otherwise a second source would
        # silently overwrite the archive's established canonical name.
        work_values: dict[str, object] = {
            "title": batch.work.title,
            "sort_title": sort_title_for(batch.work.title),
            "updated_at": ctx.now,
        }
        if batch.work.original_title is not None:
            work_values["original_title"] = batch.work.original_title
        if batch.work.release_year is not None:
            work_values["release_year"] = batch.work.release_year
        if batch.work.sequence_number is not None:
            work_values["sequence_number"] = batch.work.sequence_number
        if batch.work.image_url is not None:
            work_values["image_url"] = batch.work.image_url
        if batch.work.metadata:
            work_values["metadata"] = batch.work.metadata
        await conn.execute(update(works).where(works.c.id == work_id).values(work_values))
    item_id = await _upsert_provider_item(conn, ctx, raw, batch, work_id)
    if batch.work.image_url:
        # Backfill artwork onto a work this run did not create: a matched, manually linked, or
        # pre-artwork-support work carries NULL forever otherwise. Only when NULL — an existing
        # URL is another provider's stated payload, and resync must not churn it .
        await conn.execute(
            update(works)
            .where(works.c.id == work_id, works.c.image_url.is_(None))
            .values(image_url=batch.work.image_url, updated_at=ctx.now)
        )
        await register_source_on_connection(conn, batch.work.image_url)
    await queue_work_ambiguity(
        conn,
        provider_id=ctx.provider_id,
        provider_item_id=item_id,
        batch=batch,
        candidates=resolution.ambiguous_candidates,
        now=ctx.now,
    )

    for external in batch.external_ids:
        await _upsert_external_id(
            conn, ctx, work_id, external.namespace, external.value, external.confidence
        )

    creators = await resolve_creators(
        conn,
        batch,
        family_of(batch.work.media_type),
        source=ctx.provider_id,
        now=ctx.now,
        memo=creator_memo,
    )
    await queue_cross_family_creator_suggestions(
        conn,
        provider_id=ctx.provider_id,
        provider_item_id=item_id,
        batch=batch,
        resolutions=creators,
        family=family_of(batch.work.media_type),
        now=ctx.now,
    )
    for credit, creator in zip(batch.credits, creators, strict=True):
        await conn.execute(
            upsert_stmt(
                conn,
                work_credits,
                [
                    {
                        "work_id": work_id,
                        "creator_id": creator.creator_id,
                        "role": str(credit.role),
                        "role_raw": credit.role_raw,
                        "credited_as": credit.credited_as,
                        "position": credit.position,
                        "source": ctx.provider_id,
                        "link_confidence": str(creator.confidence),
                    }
                ],
                constraint="uq_work_credits_work_creator_role_source",
                update_columns=["role_raw", "credited_as", "position", "link_confidence"],
            )
        )

    for entry in batch.entries:
        if await _upsert_entry(conn, ctx, work_id, item_id, entry):
            counts.entries_written += 1
    if batch.retracts_entries:
        counts.entries_retracted += await _retract_entries(conn, item_id, now=ctx.now)

    for opinion in batch.opinions:
        await _upsert_opinion(conn, ctx, work_id, item_id, opinion)
        counts.opinions_written += 1

    # In the same transaction as the rows above (research.md ).
    await rebuild_work_document(conn, work_id)
    for position, opinion in enumerate(batch.opinions):
        if opinion.review_text:
            await index_document(
                conn,
                SearchKind.REVIEW_TEXT,
                f"{item_id}:{position}",
                opinion.review_text,
            )


def _validate(ctx: WriteContext, batch: NormalizedBatch) -> None:
    """Re-validate what the child sent, at the boundary, before anything is written.

    The Pydantic models already rejected most of this on the way in, but the child process runs
    plugin code and this is the parent. Re-checking the things a plugin could get wrong is cheap;
    trusting them is how a bad ``subject_ref`` reaches storage (, research.md ).
    """
    for entry in batch.entries:
        # validate_subject_ref, not the model's own type: the parent-side check is the one that
        # counts, and it rejects an empty object rather than treating it as "the whole work".
        validate_subject_ref(entry.subject_ref.as_dict() if entry.subject_ref else None)

    for opinion in batch.opinions:
        validate_subject_ref(opinion.subject_ref.as_dict() if opinion.subject_ref else None)
        if opinion.rating_raw is None:
            continue
        scale_id = opinion.rating_scale_id
        if scale_id is None:  # pragma: no cover - the model already forbids this
            raise ValidationRejection("rating_raw without rating_scale_id")
        if scale_id not in ctx.rating_scales:
            raise ValidationRejection(
                f"opinion names rating scale {scale_id!r}, which provider "
                f"{ctx.provider_id!r} does not declare; the normalized value would be uncomputable"
            )


def _canonicalize_batch_identifiers(batch: NormalizedBatch) -> NormalizedBatch:
    """Canonicalize identifiers before resolution so lookup and storage share one key."""
    external_ids = [
        identifier.model_copy(
            update=dict(
                zip(
                    ("namespace", "value"),
                    canonicalize_identifier(identifier.namespace, identifier.value),
                    strict=True,
                )
            )
        )
        for identifier in batch.external_ids
    ]
    creator_external_ids = [
        identifier.model_copy(
            update=dict(
                zip(
                    ("namespace", "value"),
                    canonicalize_identifier(identifier.namespace, identifier.value),
                    strict=True,
                )
            )
        )
        for identifier in batch.creator_external_ids
    ]
    return batch.model_copy(
        update={"external_ids": external_ids, "creator_external_ids": creator_external_ids}
    )


def _normalized_rating(ctx: WriteContext, opinion: Any) -> int | None:
    """Derive the 0–100 value.

    Recomputable from stored data, so a corrected scale is replayable (research.md ).
    """
    if opinion.rating_raw is None:
        return None
    return normalize_rating(opinion.rating_raw, ctx.rating_scales[opinion.rating_scale_id])


def sort_title_for(title: str) -> str:
    """Casefold and strip a leading article, for the sort and name-matching index.

    Deliberately English-only and deliberately dumb. A real multilingual article list is a research
    project, and this value is a sort key rather than an identity: getting it wrong reorders a list,
    it does not merge two works.  replaces it with the normalizer resolution shares.
    """
    return normalize_title(title)


async def _upsert_provider_item(
    conn: AsyncConnection,
    ctx: WriteContext,
    raw: RawRecord,
    batch: NormalizedBatch,
    work_id: uuid.UUID,
) -> int:
    """Insert or refresh the provider item. ``(provider_id, native_id)`` is the idempotency key."""
    await conn.execute(
        upsert_stmt(
            conn,
            provider_items,
            [
                {
                    "provider_id": ctx.provider_id,
                    "native_id": raw.native_id,
                    "work_id": work_id,
                    "title_as_given": batch.work.title,
                    "raw_payload": raw.payload,
                    "schema_version": ctx.schema_version,
                    "first_seen_at": ctx.now,
                    "last_seen_at": ctx.now,
                }
            ],
            constraint="uq_provider_items_provider_id_native_id",
            # first_seen_at is NOT updated: it is the earliest sighting, and overwriting it on every
            # resync would quietly erase when the archive first learned of the item.
            update_columns=[
                "work_id",
                "title_as_given",
                "raw_payload",
                "schema_version",
                "last_seen_at",
            ],
        )
    )
    result = await conn.execute(
        select(provider_items.c.id).where(
            and_(
                provider_items.c.provider_id == ctx.provider_id,
                provider_items.c.native_id == raw.native_id,
            )
        )
    )
    return int(result.scalar_one())


async def _upsert_external_id(
    conn: AsyncConnection,
    ctx: WriteContext,
    work_id: uuid.UUID,
    namespace: str,
    value: str,
    confidence: Confidence,
) -> None:
    """Record an identifier. Every one the payload carried, including ones we have no use for."""
    await conn.execute(
        upsert_stmt(
            conn,
            external_ids,
            [
                {
                    "work_id": work_id,
                    "namespace": namespace,
                    "value": value.strip(),
                    "source": ctx.provider_id,
                    "confidence": str(confidence),
                    "created_at": ctx.now,
                }
            ],
            index_elements=["namespace", "value"],
            # Nothing to update: an identifier is a fact, and re-asserting it should not rewrite who
            # said it first.
            update_columns=None,
        )
    )


async def _retract_entries(conn: AsyncConnection, item_id: int, *, now: datetime) -> int:
    """Tombstone what this provider item logged, because the provider says it was withdrawn.

    Reached only from ``NormalizedBatch.retracts_entries`` — never inferred from an empty
    ``entries`` list, which is ambiguous (see that field). A platform whose records are mutable
    *state* rather than immutable *events* can take back what it once said: an AniList list entry
    moved to ``PLANNING`` states that the account has watched none of it, so the watch a previous
    sync recorded from that same item is no longer attested. The upsert path covers every status
    that still yields an entry — it rewrites ``kind``, ``logged_at`` and the progress columns on the
    same ``(provider_id, native_id)`` row — but a withdrawal writes nothing, and the stale row would
    otherwise survive forever.

    Narrower than :func:`infer_deletes` in the two ways that let it run without those guards: the
    provider states the withdrawal for a specific record instead of the host deducing it from
    absence, and the scope is one ``provider_item_id`` rather than every item a run did not see.
    Another platform's entry for the same work is untouched.

    A tombstone, never a delete : the row keeps its history, drops out of the log and
    ``entry_count`` by default, and the next sync that does yield an entry clears ``deleted_at``
    again through the upsert's update columns.
    """
    result = await conn.execute(
        update(entries)
        .where(entries.c.provider_item_id == item_id, entries.c.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    return int(result.rowcount)


async def _upsert_entry(
    conn: AsyncConnection,
    ctx: WriteContext,
    work_id: uuid.UUID,
    item_id: int,
    entry: Any,
) -> bool:
    """Write one logged event idempotently. Returns whether a row was created.

    Two paths, because platforms differ in whether they identify their own events:

    * ``native_id`` present — the partial unique index does the work, and ``ON CONFLICT`` updates.
    * ``native_id`` absent — there is no key to conflict on, so the writer looks for an existing row
      matching ``(provider_item_id, kind, logged_at, subject_ref)`` and skips if it finds one. This
      is the fallback data-model.md §2 requires; without it a feed with no event ids duplicates its
      whole history on every resync .
    """
    subject = entry.subject_ref.as_dict() if entry.subject_ref else None
    values = {
        "work_id": work_id,
        "provider_id": ctx.provider_id,
        "provider_item_id": item_id,
        "native_id": entry.native_id,
        "kind": str(entry.kind),
        "logged_at": entry.logged_at,
        "logged_precision": str(entry.logged_precision),
        "subject_ref": subject,
        "progress_value": entry.progress_value,
        "progress_unit": entry.progress_unit,
        "subject_ref_key": _subject_ref_key(subject) if entry.native_id is None else None,
        "metadata": entry.metadata,
        "ingested_at": ctx.now,
        "deleted_at": None,
    }

    if entry.native_id is not None:
        await conn.execute(
            upsert_stmt(
                conn,
                entries,
                [values],
                index_elements=["provider_id", "native_id"],
                # The index is PARTIAL, and both dialects require the target to repeat that
                # predicate. Omitting it fails with "ON CONFLICT clause does not match any
                # PRIMARY KEY or UNIQUE constraint" even though the index is right there.
                index_where=entries.c.native_id.isnot(None),
                update_columns=[
                    "work_id",
                    "provider_item_id",
                    "kind",
                    "logged_at",
                    "logged_precision",
                    "subject_ref",
                    "progress_value",
                    "progress_unit",
                    "metadata",
                    "deleted_at",
                ],
            )
        )
        return True

    await conn.execute(
        upsert_stmt(
            conn,
            entries,
            [values],
            index_elements=["provider_item_id", "kind", "logged_at", "subject_ref_key"],
            index_where=and_(entries.c.native_id.is_(None), entries.c.subject_ref_key.is_not(None)),
            update_columns=[
                "work_id",
                "provider_id",
                "subject_ref",
                "progress_value",
                "progress_unit",
                "metadata",
                "logged_precision",
                "deleted_at",
                "ingested_at",
            ],
        )
    )
    return True


def _subject_ref_key(subject: dict[str, int] | None) -> str:
    """Return one deterministic, portable representation for an optional subject reference."""
    return json.dumps(subject, sort_keys=True, separators=(",", ":")) if subject else "{}"


async def _upsert_opinion(
    conn: AsyncConnection,
    ctx: WriteContext,
    work_id: uuid.UUID,
    item_id: int,
    opinion: Any,
) -> None:
    """Write a rating, like, or review. ``(provider_id, provider_item_id)`` is the key.

    Per-sub-unit ratings arrive as distinct ``provider_items``, so they do not collide on that key —
    which is why an episode rating and a series rating can coexist.
    """
    await conn.execute(
        upsert_stmt(
            conn,
            opinions,
            [
                {
                    "work_id": work_id,
                    "provider_id": ctx.provider_id,
                    "provider_item_id": item_id,
                    "rating_raw": opinion.rating_raw,
                    "rating_scale_id": opinion.rating_scale_id,
                    "rating_normalized": _normalized_rating(ctx, opinion),
                    "subject_ref": opinion.subject_ref.as_dict() if opinion.subject_ref else None,
                    "is_liked": opinion.is_liked,
                    "review_text": opinion.review_text,
                    "review_format": (
                        str(opinion.review_format) if opinion.review_format else None
                    ),
                    "contains_spoilers": opinion.contains_spoilers,
                    "authored_at": opinion.authored_at,
                    "updated_at": ctx.now,
                    "deleted_at": None,
                }
            ],
            constraint="uq_opinions_provider_id_provider_item_id",
            update_columns=[
                "work_id",
                "rating_raw",
                "rating_scale_id",
                "rating_normalized",
                "subject_ref",
                "is_liked",
                "review_text",
                "review_format",
                "contains_spoilers",
                "authored_at",
                "updated_at",
                "deleted_at",
            ],
        )
    )


async def ensure_rating_scales(conn: AsyncConnection, scales: Sequence[RatingScale]) -> None:
    """Store the scales a provider declares, so ``rating_normalized`` stays recomputable.

    Without the scale definition in the database, a corrected scale could not be replayed and the
    normalized values would be unrepairable without re-syncing every platform (research.md ).
    """
    from aggregato.db.schema import rating_scales

    for scale in scales:
        values = {
            "id": scale.id,
            "min_value": scale.min_value,
            "max_value": scale.max_value,
            "step": scale.step,
            "kind": str(scale.kind),
            "labels": scale.labels,
        }
        row = (
            await conn.execute(select(rating_scales).where(rating_scales.c.id == scale.id))
        ).first()
        if row is None:
            await conn.execute(rating_scales.insert().values(values))
            continue
        if any(
            getattr(row, name) != values[name]
            for name in ("min_value", "max_value", "step", "kind", "labels")
        ):
            raise ValueError(
                f"rating scale {scale.id!r} is immutable and its stored definition conflicts"
            )


__all__ = [
    "SubjectRef",
    "ValidationRejection",
    "WriteContext",
    "WriteCounts",
    "ensure_rating_scales",
    "sort_title_for",
    "write_batches",
]
