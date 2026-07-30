"""Replay retained raw payloads after a provider changes its normalization schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import transaction
from aggregato.db.schema import entries, opinions, provider_items
from aggregato.domain.models import RawRecord


async def records_needing_replay(
    engine: AsyncEngine, *, provider_id: str, schema_version: int
) -> list[RawRecord]:
    """Return retained payloads only when the declared version exceeds their stored maximum."""
    async with transaction(engine) as conn:
        maximum = (
            await conn.execute(
                select(func.max(provider_items.c.schema_version)).where(
                    provider_items.c.provider_id == provider_id
                )
            )
        ).scalar_one()
        if maximum is None or int(maximum) >= schema_version:
            return []
        rows = await conn.execute(
            select(provider_items.c.native_id, provider_items.c.raw_payload).where(
                provider_items.c.provider_id == provider_id
            )
        )
    return [RawRecord(native_id=str(row.native_id), payload=dict(row.raw_payload)) for row in rows]


async def tombstone_replay_derivatives(
    engine: AsyncEngine, *, provider_id: str, native_ids: list[str], now: datetime
) -> None:
    """Retire old facts; the replay writer revives precisely the facts it still emits."""
    if not native_ids:
        return
    item_ids = select(provider_items.c.id).where(
        provider_items.c.provider_id == provider_id,
        provider_items.c.native_id.in_(native_ids),
    )
    async with transaction(engine) as conn:
        await conn.execute(
            update(entries).where(entries.c.provider_item_id.in_(item_ids)).values(deleted_at=now)
        )
        await conn.execute(
            update(opinions).where(opinions.c.provider_item_id.in_(item_ids)).values(deleted_at=now)
        )
