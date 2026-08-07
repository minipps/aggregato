"""Replay retained raw payloads after a provider changes its normalization schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import transaction
from aggregato.db.schema import entries, opinions, provider_items
from aggregato.db.search import SearchKind
from aggregato.domain.models import RawRecord


async def records_needing_replay(
    engine: AsyncEngine, *, provider_id: str, schema_version: int
) -> list[RawRecord]:
    """Return every retained payload whose own schema version is stale."""
    async with transaction(engine) as conn:
        rows = await conn.execute(
            select(provider_items.c.native_id, provider_items.c.raw_payload).where(
                provider_items.c.provider_id == provider_id,
                provider_items.c.schema_version < schema_version,
            )
        )
    return [RawRecord(native_id=str(row.native_id), payload=dict(row.raw_payload)) for row in rows]


async def tombstone_replay_derivatives(
    engine: AsyncEngine, *, provider_id: str, native_ids: list[str], now: datetime
) -> None:
    """Retire old facts; the replay writer revives precisely the facts it still emits."""
    if not native_ids:
        return
    async with transaction(engine) as conn:
        item_id_rows = await conn.execute(
            select(provider_items.c.id).where(
                provider_items.c.provider_id == provider_id,
                provider_items.c.native_id.in_(native_ids),
            )
        )
        item_ids = [int(row.id) for row in item_id_rows]
        await conn.execute(
            update(entries).where(entries.c.provider_item_id.in_(item_ids)).values(deleted_at=now)
        )
        await conn.execute(
            update(opinions).where(opinions.c.provider_item_id.in_(item_ids)).values(deleted_at=now)
        )
        # Review documents are derivatives of the opinions being tombstoned. The writer will
        # replace them for opinions emitted by the corrected normalizer; remove every old position
        # first so a dropped review cannot remain searchable after replay.
        for item_id in item_ids:
            await conn.execute(
                text("DELETE FROM search_index WHERE kind = :kind AND ref_id LIKE :ref_prefix"),
                {
                    "kind": str(SearchKind.REVIEW_TEXT),
                    "ref_prefix": f"{item_id}:%",
                },
            )
