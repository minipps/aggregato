"""Conservative work identity resolution .

The order is deliberate: provider-asserted identifiers are proof, an exact title/year key is only
usable when it has one candidate, and every ambiguity creates a separate work plus a queue item.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import external_ids, works
from aggregato.domain.enums import Confidence
from aggregato.domain.models import NormalizedBatch
from aggregato.ingest.titles import normalize_title


@dataclass(frozen=True, slots=True)
class WorkResolution:
    """Resolved work id and candidates that require operator review."""

    work_id: uuid.UUID
    confidence: Confidence
    ambiguous_candidates: tuple[uuid.UUID, ...] = ()


async def resolve_work(
    conn: AsyncConnection, batch: NormalizedBatch, *, now: datetime
) -> WorkResolution:
    """Resolve asserted id → unique exact title/year → create, never guessing across ambiguity."""
    for external in batch.external_ids:
        row = (
            await conn.execute(
                select(external_ids.c.work_id).where(
                    external_ids.c.namespace == external.namespace,
                    external_ids.c.value == external.value,
                )
            )
        ).first()
        if row is not None:
            return WorkResolution(row.work_id, Confidence.ASSERTED)

    # A provider that asserts an identifier has already said which work this is.  If that identifier
    # is new, title/year must not override it: two differently identified works can share a title
    # and year (different cuts, remakes, or simply a provider correction).  Title matching is the
    # fallback only when the payload offered no identifier at all.
    if batch.external_ids:
        return await _create_work(conn, batch, now=now)

    title_key = normalize_title(batch.work.title)
    candidates = list(
        await conn.execute(
            select(works.c.id).where(
                and_(
                    works.c.media_type == str(batch.work.media_type),
                    works.c.sort_title == title_key,
                    works.c.release_year == batch.work.release_year,
                )
            )
        )
    )
    if len(candidates) == 1:
        return WorkResolution(candidates[0].id, Confidence.MATCHED)

    return await _create_work(
        conn, batch, now=now, candidates=tuple(candidate.id for candidate in candidates)
    )


async def _create_work(
    conn: AsyncConnection,
    batch: NormalizedBatch,
    *,
    now: datetime,
    candidates: tuple[uuid.UUID, ...] = (),
) -> WorkResolution:
    """Create an unlinked work and retain any ambiguous title candidates for the queue."""
    work_id = uuid.uuid4()
    await conn.execute(
        works.insert().values(
            id=work_id,
            media_type=str(batch.work.media_type),
            title=batch.work.title,
            sort_title=normalize_title(batch.work.title),
            original_title=batch.work.original_title,
            release_year=batch.work.release_year,
            sequence_number=batch.work.sequence_number,
            image_url=batch.work.image_url,
            metadata=batch.work.metadata,
            created_at=now,
            updated_at=now,
        )
    )
    return WorkResolution(work_id, Confidence.ASSERTED, candidates)
