"""Persist conservative identity ambiguities for an operator to decide."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import creator_aliases, creators, resolution_queue
from aggregato.domain.enums import MediaFamily, ResolutionSubject
from aggregato.domain.models import NormalizedBatch
from aggregato.ingest.resolve_creator import CreatorResolution
from aggregato.ingest.titles import normalize_title


async def supersede_stale_open_items(
    conn: AsyncConnection,
    *,
    provider_id: str,
    refreshed_at: datetime,
    now: datetime,
) -> None:
    """Close items superseded by a successful full refresh while preserving their history."""
    await conn.execute(
        update(resolution_queue)
        .where(
            resolution_queue.c.provider_id == provider_id,
            resolution_queue.c.decided_at.is_(None),
            resolution_queue.c.created_at < refreshed_at,
        )
        .values(decided_at=now, decision="ignored")
    )


async def queue_work_ambiguity(
    conn: AsyncConnection,
    *,
    provider_id: str,
    provider_item_id: int,
    batch: NormalizedBatch,
    candidates: tuple[uuid.UUID, ...],
    now: datetime,
) -> None:
    """Store a title/year collision, reasons, and the separate work proposed by ingestion."""
    if not candidates:
        return
    await conn.execute(
        resolution_queue.insert().values(
            subject=str(ResolutionSubject.WORK),
            provider_id=provider_id,
            payload_ref=provider_item_id,
            candidates=[
                {"id": str(candidate), "reason": "same media type, normalized title, and year"}
                for candidate in candidates
            ],
            proposed={
                "title": batch.work.title,
                "media_type": str(batch.work.media_type),
                "release_year": batch.work.release_year,
            },
            created_at=now,
            suggestion_kind="ambiguous_title_year",
        )
    )


async def queue_cross_family_creator_suggestions(
    conn: AsyncConnection,
    *,
    provider_id: str,
    provider_item_id: int | None,
    batch: NormalizedBatch,
    resolutions: list[CreatorResolution],
    family: MediaFamily,
    now: datetime,
) -> None:
    """Surface same-name creators in another family without joining them.

    A family-local lookup in :func:`resolve_creators` intentionally creates a separate creator.
    This records the potentially useful cross-family relationship for operator review, preserving
    the candidate's alias and family as the reason it was suggested.  Replaying a provider item
    does not duplicate an open suggestion for the same proposed creator.
    """
    names = {normalize_title(credit.creator_name) for credit in batch.credits}
    if not names:
        return
    rows = await conn.execute(
        select(
            creator_aliases.c.normalized,
            creator_aliases.c.creator_id,
            creator_aliases.c.name,
            creator_aliases.c.media_family,
            creators.c.name.label("creator_name"),
        )
        .select_from(creator_aliases.join(creators))
        .where(
            creator_aliases.c.normalized.in_(names),
            creator_aliases.c.media_family != str(family),
        )
    )
    by_name: dict[str, dict[uuid.UUID, Any]] = {}
    for row in rows:
        by_name.setdefault(row.normalized, {})[row.creator_id] = row

    existing_stmt = select(resolution_queue.c.proposed).where(
        resolution_queue.c.subject == str(ResolutionSubject.CREATOR),
        resolution_queue.c.suggestion_kind == "cross_family_name",
        resolution_queue.c.decided_at.is_(None),
    )
    if provider_item_id is None:
        existing_stmt = existing_stmt.where(resolution_queue.c.payload_ref.is_(None))
    else:
        existing_stmt = existing_stmt.where(resolution_queue.c.payload_ref == provider_item_id)
    existing = {
        str(row.proposed.get("creator_id"))
        for row in await conn.execute(existing_stmt)
        if isinstance(row.proposed, dict) and row.proposed.get("creator_id") is not None
    }

    for credit, resolution in zip(batch.credits, resolutions, strict=True):
        candidates = [
            row
            for creator_id, row in by_name.get(normalize_title(credit.creator_name), {}).items()
            if creator_id != resolution.creator_id
        ]
        if not candidates or str(resolution.creator_id) in existing:
            continue
        await conn.execute(
            resolution_queue.insert().values(
                subject=str(ResolutionSubject.CREATOR),
                provider_id=provider_id,
                payload_ref=provider_item_id,
                candidates=[
                    {
                        "id": str(row.creator_id),
                        "reason": "same normalized name in another media family",
                        "name": row.creator_name,
                        "media_family": row.media_family,
                    }
                    for row in candidates
                ],
                proposed={
                    "creator_id": str(resolution.creator_id),
                    "name": credit.creator_name,
                    "media_family": str(family),
                },
                created_at=now,
                suggestion_kind="cross_family_name",
            )
        )
        existing.add(str(resolution.creator_id))
