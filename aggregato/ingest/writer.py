"""The validated writer — the only thing in the system that writes ingested data (FR-008).

Everything a provider produces passes through here, and it is validated **in this process**, never
trusted from the child: the child runs plugin code (research.md R2, R17). A record that fails
validation becomes an ``ingest_failure`` with its payload, and the run continues (FR-023).

Three properties this module exists to guarantee:

**Idempotency (FR-005).** A resync writes nothing new. For rows the platform identifies, that is a
unique constraint plus ``ON CONFLICT`` — never a pre-``SELECT``, which races. For entries the
platform gives no event id, there is no key to conflict on, so the writer deduplicates on
``(provider_item_id, kind, logged_at, subject_ref)`` itself. That fallback is the interesting half:
without it, every resync of a feed without event ids would duplicate the entire history.

**Search stays in step.** The search index is written in the **same transaction** as the row it
describes (research.md R5). A rolled-back write cannot leave a searchable ghost, and there is one
implementation of *when* to index rather than two dialects' worth of trigger DDL.

**Nothing is destroyed.** Deletes are tombstones (``deleted_at``), never row removal, and the
inferred-delete path is guarded three ways over (see :func:`infer_deletes`, T088 completes it).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import (
    entries,
    external_ids,
    opinions,
    provider_items,
    work_credits,
)
from aggregato.db.search import SearchKind, index_document
from aggregato.db.upsert import upsert_stmt
from aggregato.domain.enums import Confidence, IngestStage
from aggregato.domain.families import family_of
from aggregato.domain.models import NormalizedBatch, RawRecord
from aggregato.domain.ratings import RatingOutOfScale, RatingScale, normalize_rating
from aggregato.domain.subject_ref import SubjectRef, validate_subject_ref
from aggregato.images.cache import register_source_on_connection
from aggregato.ingest.failures import capture_failure
from aggregato.ingest.resolve_creator import resolve_creators
from aggregato.ingest.resolve_queue import (
    queue_cross_family_creator_suggestions,
    queue_work_ambiguity,
)
from aggregato.ingest.resolve_work import resolve_work
from aggregato.ingest.titles import normalize_title


@dataclass
class WriteCounts:
    """What one run actually did, for the ``sync_runs`` row and the operator's history."""

    seen: int = 0
    written: int = 0
    failed: int = 0
    entries_written: int = 0
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
            because ``provider_items.raw_payload`` stores it as the replay source (FR-002).

    Returns:
        Counts for the run row.
    """
    counts = WriteCounts()
    for raw, batch in records:
        counts.seen += 1
        try:
            await _write_one(conn, ctx, raw, batch, counts)
        except (ValidationRejection, RatingOutOfScale, ValueError) as exc:
            # One poisoned record must never cost the operator the rest of the run (FR-023).
            counts.failed += 1
            failure_id = await capture_failure(
                conn,
                provider_id=ctx.provider_id,
                sync_run_id=ctx.sync_run_id,
                stage=IngestStage.VALIDATE,
                error=exc,
                raw_payload=raw.payload,
                now=ctx.now,
            )
            counts.failure_ids.append(failure_id)
        else:
            counts.written += 1
    return counts


async def _write_one(
    conn: AsyncConnection,
    ctx: WriteContext,
    raw: RawRecord,
    batch: NormalizedBatch,
    counts: WriteCounts,
) -> None:
    """Write one normalized batch. Raises rather than half-writing."""
    _validate(ctx, batch)

    resolution = await resolve_work(conn, batch, now=ctx.now)
    work_id = resolution.work_id
    item_id = await _upsert_provider_item(conn, ctx, raw, batch, work_id)
    if batch.work.image_url:
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
        conn, batch, family_of(batch.work.media_type), source=ctx.provider_id, now=ctx.now
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

    for opinion in batch.opinions:
        await _upsert_opinion(conn, ctx, work_id, item_id, opinion)
        counts.opinions_written += 1

    # In the same transaction as the rows above (research.md R5).
    await index_document(
        conn,
        SearchKind.WORK_TITLE,
        str(work_id),
        _title_document(batch),
    )
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
    trusting them is how a bad ``subject_ref`` reaches storage (FR-008, research.md R17).
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


def _normalized_rating(ctx: WriteContext, opinion: Any) -> int | None:
    """Derive the 0–100 value.

    Recomputable from stored data, so a corrected scale is replayable (research.md R7).
    """
    if opinion.rating_raw is None:
        return None
    return normalize_rating(opinion.rating_raw, ctx.rating_scales[opinion.rating_scale_id])


def _title_document(batch: NormalizedBatch) -> str:
    """Every title form in one document.

    So that searching an original-language title finds a work logged under its translation.
    """
    forms = [batch.work.title]
    if batch.work.original_title and batch.work.original_title != batch.work.title:
        forms.append(batch.work.original_title)
    return " ".join(forms)


def sort_title_for(title: str) -> str:
    """Casefold and strip a leading article, for the sort and name-matching index.

    Deliberately English-only and deliberately dumb. A real multilingual article list is a research
    project, and this value is a sort key rather than an identity: getting it wrong reorders a list,
    it does not merge two works. T070 replaces it with the normalizer resolution shares.
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
            constraint="uq_external_ids_namespace_value_work",
            # Nothing to update: an identifier is a fact, and re-asserting it should not rewrite who
            # said it first.
            update_columns=None,
        )
    )


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
      whole history on every resync (FR-005, SC-003).
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
        "metadata": entry.metadata,
        "ingested_at": ctx.now,
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
                ],
            )
        )
        return True

    existing = await conn.execute(
        select(entries.c.id).where(
            and_(
                entries.c.provider_item_id == item_id,
                entries.c.kind == str(entry.kind),
                entries.c.logged_at == entry.logged_at,
                _subject_ref_matches(subject),
            )
        )
    )
    if existing.first() is not None:
        return False
    await conn.execute(entries.insert().values(values))
    return True


def _subject_ref_matches(subject: dict[str, int] | None) -> Any:
    """Compare ``subject_ref`` for the no-native-id fallback.

    JSON equality is not portable across dialects, so this compares against the stored JSON value
    directly for the null case and by equality otherwise. ``SubjectRef.as_dict()`` omits unset keys,
    which is what makes the non-null comparison stable: ``{"track": 7}`` has exactly one
    representation rather than one per combination of explicit nulls.
    """
    if subject is None:
        return entries.c.subject_ref.is_(None)
    return entries.c.subject_ref == subject


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
            ],
        )
    )


async def ensure_rating_scales(conn: AsyncConnection, scales: Sequence[RatingScale]) -> None:
    """Store the scales a provider declares, so ``rating_normalized`` stays recomputable.

    Without the scale definition in the database, a corrected scale could not be replayed and the
    normalized values would be unrepairable without re-syncing every platform (research.md R7).
    """
    from aggregato.db.schema import rating_scales

    if not scales:
        return
    await conn.execute(
        upsert_stmt(
            conn,
            rating_scales,
            [
                {
                    "id": scale.id,
                    "min_value": scale.min_value,
                    "max_value": scale.max_value,
                    "step": scale.step,
                    "kind": str(scale.kind),
                    "labels": scale.labels,
                }
                for scale in scales
            ],
            index_elements=["id"],
            update_columns=["min_value", "max_value", "step", "kind", "labels"],
        )
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
