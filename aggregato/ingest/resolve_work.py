"""Conservative work identity resolution (T071).

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

    work_id = uuid.uuid4()
    await conn.execute(
        works.insert().values(
            id=work_id,
            media_type=str(batch.work.media_type),
            title=batch.work.title,
            sort_title=title_key,
            original_title=batch.work.original_title,
            release_year=batch.work.release_year,
            sequence_number=batch.work.sequence_number,
            image_url=batch.work.image_url,
            metadata=batch.work.metadata,
            created_at=now,
            updated_at=now,
        )
    )
    return WorkResolution(
        work_id,
        Confidence.ASSERTED,
        tuple(candidate.id for candidate in candidates),
    )
