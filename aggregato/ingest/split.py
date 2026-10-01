"""Credit-granular, reversible creator splits."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import creators, merge_log, work_credits, works
from aggregato.domain.enums import Confidence, ResolutionSubject
from aggregato.ingest.merge import _snapshot
from aggregato.ingest.titles import normalize_title


async def split_creator(
    conn: AsyncConnection,
    *,
    creator_id: uuid.UUID,
    credit_ids: list[int],
    new_name: str | None,
    now: datetime,
) -> Any:
    """Create a separate creator and move precisely the selected credits to it."""
    if not credit_ids or len(set(credit_ids)) != len(credit_ids):
        raise ValueError("credit_ids must be non-empty and unique")
    source = (await conn.execute(select(creators).where(creators.c.id == creator_id))).first()
    if source is None:
        raise LookupError("unknown creator")
    selected = list(
        await conn.execute(select(work_credits).where(work_credits.c.id.in_(credit_ids)))
    )
    if len(selected) != len(credit_ids) or any(
        credit.creator_id != creator_id for credit in selected
    ):
        raise LookupError("one or more credits do not belong to this creator")
    work_ids = {credit.work_id for credit in selected}
    await conn.execute(
        select(works.c.id).where(works.c.id.in_(work_ids)).order_by(works.c.id).with_for_update()
    )
    credits = list(
        await conn.execute(
            select(work_credits).where(
                work_credits.c.id.in_(credit_ids), work_credits.c.creator_id == creator_id
            )
        )
    )
    if len(credits) != len(credit_ids):
        raise LookupError("one or more credits do not belong to this creator")
    new_id = uuid.uuid4()
    snapshot = await _snapshot(
        conn,
        {
            "creators": (creators, creators.c.id == creator_id),
            "work_credits": (work_credits, work_credits.c.id.in_(credit_ids)),
        },
    )
    name = (new_name or source.name).strip()
    if not name:
        raise ValueError("new_name cannot be empty")
    await conn.execute(
        creators.insert().values(
            id=new_id,
            kind=source.kind,
            name=name,
            sort_name=normalize_title(name),
            image_url=None,
            metadata={},
            created_at=now,
            updated_at=now,
        )
    )
    for credit in credits:
        await conn.execute(
            update(work_credits)
            .where(work_credits.c.id == credit.id)
            .values(
                creator_id=new_id,
                link_confidence=str(Confidence.MANUAL),
                manual_from_creator_id=credit.manual_from_creator_id or creator_id,
            )
        )
    return (
        await conn.execute(
            merge_log.insert()
            .values(
                subject=str(ResolutionSubject.CREATOR),
                operation="split",
                winner_id=new_id,
                loser_ids=[str(creator_id)],
                moved_credit_ids=credit_ids,
                performed_at=now,
                snapshot=snapshot,
            )
            .returning(merge_log)
        )
    ).one()
