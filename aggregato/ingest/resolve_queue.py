"""Persist conservative identity ambiguities for an operator to decide (T073)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import resolution_queue
from aggregato.domain.enums import ResolutionSubject
from aggregato.domain.models import NormalizedBatch


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
