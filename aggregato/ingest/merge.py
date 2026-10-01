"""Merge work and creator identities with an undo log.

The merge log keeps a JSON-safe copy of every row the operation changes.  This is deliberately a
snapshot rather than a lossy "inverse" instruction: a merge can touch identifiers, provider items,
and several kinds of child records. The merge log stores the prior rows needed for a guarded undo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import (
    creator_aliases,
    creator_external_ids,
    creators,
    entries,
    external_ids,
    merge_log,
    opinions,
    provider_items,
    work_credits,
    works,
)
from aggregato.db.search import SearchKind, rebuild_work_document, unindex_document
from aggregato.domain.enums import ResolutionSubject


async def merge_works(
    conn: AsyncConnection, *, winner_id: uuid.UUID, loser_ids: list[uuid.UUID], now: datetime
) -> Any:
    """Move every work-owned record to ``winner_id`` and retire the duplicates."""
    ids = _merge_ids(winner_id, loser_ids)
    await _require_rows(conn, works, ids, "work")
    await _ensure_parent_merge_safe(conn, winner_id, set(loser_ids))
    snapshot = await _work_snapshot(conn, ids)
    losers = ids[1:]
    for loser in losers:
        await _remove_work_credit_collisions(conn, loser, winner_id)
        await conn.execute(
            update(entries).where(entries.c.work_id == loser).values(work_id=winner_id)
        )
        await conn.execute(
            update(opinions).where(opinions.c.work_id == loser).values(work_id=winner_id)
        )
        await conn.execute(
            update(provider_items)
            .where(provider_items.c.work_id == loser)
            .values(work_id=winner_id)
        )
        await conn.execute(
            update(work_credits).where(work_credits.c.work_id == loser).values(work_id=winner_id)
        )
        await conn.execute(
            update(works).where(works.c.parent_work_id == loser).values(parent_work_id=winner_id)
        )
        # Identical asserted IDs need only one live copy after the merge. Their pre-merge rows are
        # in the snapshot, so undo restores provenance exactly.
        winner_external_ids = external_ids.alias("winner_external_ids")
        duplicate_ids = select(external_ids.c.id).where(
            external_ids.c.work_id == loser,
            exists(
                select(1).where(
                    winner_external_ids.c.work_id == winner_id,
                    winner_external_ids.c.namespace == external_ids.c.namespace,
                    winner_external_ids.c.value == external_ids.c.value,
                )
            ),
        )
        await conn.execute(delete(external_ids).where(external_ids.c.id.in_(duplicate_ids)))
        await conn.execute(
            update(external_ids).where(external_ids.c.work_id == loser).values(work_id=winner_id)
        )
        await unindex_document(conn, SearchKind.WORK_TITLE, str(loser))
    await conn.execute(delete(works).where(works.c.id.in_(losers)))
    await rebuild_work_document(conn, winner_id)
    return await _log(conn, ResolutionSubject.WORK, "merge", winner_id, losers, None, snapshot, now)


async def merge_creators(
    conn: AsyncConnection, *, winner_id: uuid.UUID, loser_ids: list[uuid.UUID], now: datetime
) -> Any:
    """Merge creator identities while retaining aliases and identifiers from every family."""
    ids = _merge_ids(winner_id, loser_ids)
    await _require_rows(conn, creators, ids, "creator")
    snapshot = await _creator_snapshot(conn, ids)
    losers = ids[1:]
    for loser in losers:
        await _remove_creator_credit_collisions(conn, loser, winner_id)
        await conn.execute(
            update(work_credits)
            .where(work_credits.c.creator_id == loser)
            .values(creator_id=winner_id)
        )
        # Alias / ID uniqueness is scoped to the destination identity.  Delete only an exact
        # duplicate; differing source aliases remain available to the operator after the merge.
        for table, columns in (
            (creator_aliases, ("normalized", "media_family")),
            (creator_external_ids, ("namespace", "value")),
        ):
            winner_rows = table.alias(f"winner_{table.name}")
            pair_exists = exists(
                select(1).where(
                    winner_rows.c.creator_id == winner_id,
                    *[winner_rows.c[name] == table.c[name] for name in columns],
                )
            )
            await conn.execute(delete(table).where(table.c.creator_id == loser, pair_exists))
            await conn.execute(
                update(table).where(table.c.creator_id == loser).values(creator_id=winner_id)
            )
    await conn.execute(delete(creators).where(creators.c.id.in_(losers)))
    return await _log(
        conn, ResolutionSubject.CREATOR, "merge", winner_id, losers, None, snapshot, now
    )


async def _work_snapshot(conn: AsyncConnection, ids: list[uuid.UUID]) -> dict[str, Any]:
    return await _snapshot(
        conn,
        {
            "works": (works, works.c.id.in_(ids)),
            "parent_works": (works, works.c.parent_work_id.in_(ids)),
            "external_ids": (external_ids, external_ids.c.work_id.in_(ids)),
            "provider_items": (provider_items, provider_items.c.work_id.in_(ids)),
            "entries": (entries, entries.c.work_id.in_(ids)),
            "opinions": (opinions, opinions.c.work_id.in_(ids)),
            "work_credits": (work_credits, work_credits.c.work_id.in_(ids)),
        },
    )


async def _creator_snapshot(conn: AsyncConnection, ids: list[uuid.UUID]) -> dict[str, Any]:
    return await _snapshot(
        conn,
        {
            "creators": (creators, creators.c.id.in_(ids)),
            "creator_aliases": (creator_aliases, creator_aliases.c.creator_id.in_(ids)),
            "creator_external_ids": (
                creator_external_ids,
                creator_external_ids.c.creator_id.in_(ids),
            ),
            "work_credits": (work_credits, work_credits.c.creator_id.in_(ids)),
        },
    )


async def _snapshot(conn: AsyncConnection, sources: dict[str, tuple[Any, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, (table, condition) in sources.items():
        result[name] = [
            jsonable_encoder(dict(row._mapping))
            for row in await conn.execute(select(table).where(condition))
        ]
    return result


async def _log(
    conn: AsyncConnection,
    subject: ResolutionSubject,
    operation: str,
    winner_id: uuid.UUID,
    loser_ids: list[uuid.UUID],
    moved_credit_ids: list[int] | None,
    snapshot: dict[str, Any],
    now: datetime,
) -> Any:
    return (
        await conn.execute(
            merge_log.insert()
            .values(
                subject=str(subject),
                operation=operation,
                winner_id=winner_id,
                loser_ids=[str(item) for item in loser_ids],
                moved_credit_ids=moved_credit_ids,
                performed_at=now,
                snapshot=snapshot,
            )
            .returning(merge_log)
        )
    ).one()


def _merge_ids(winner_id: uuid.UUID, loser_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    if not loser_ids or winner_id in loser_ids or len(set(loser_ids)) != len(loser_ids):
        raise ValueError("loser_ids must be non-empty, unique, and exclude the destination")
    return [winner_id, *loser_ids]


async def _require_rows(
    conn: AsyncConnection, table: Any, ids: list[uuid.UUID], label: str
) -> None:
    found = set((await conn.execute(select(table.c.id).where(table.c.id.in_(ids)))).scalars())
    if found != set(ids):
        raise LookupError(f"unknown {label}")


async def _ensure_parent_merge_safe(
    conn: AsyncConnection, winner_id: uuid.UUID, loser_ids: set[uuid.UUID]
) -> None:
    """Reject a merge that would make the winner its own ancestor."""
    current: uuid.UUID | None = winner_id
    visited: set[uuid.UUID] = set()
    while current is not None:
        if current in loser_ids:
            raise ValueError("cannot merge a work into one of its own ancestors")
        if current in visited:
            raise ValueError("work parent hierarchy already contains a cycle")
        visited.add(current)
        current = (
            await conn.execute(select(works.c.parent_work_id).where(works.c.id == current))
        ).scalar_one_or_none()


async def _remove_work_credit_collisions(
    conn: AsyncConnection, loser_id: uuid.UUID, winner_id: uuid.UUID
) -> None:
    """Avoid collapsing a unique credit key when the same credit exists on both works."""
    loser_credits = await conn.execute(
        select(work_credits).where(work_credits.c.work_id == loser_id)
    )
    for credit in loser_credits:
        collision = (
            await conn.execute(
                select(work_credits).where(
                    work_credits.c.work_id == winner_id,
                    work_credits.c.creator_id == credit.creator_id,
                    work_credits.c.role == credit.role,
                    work_credits.c.source == credit.source,
                )
            )
        ).first()
        if collision is not None:
            _ensure_credit_override_compatible(credit, collision)
            await conn.execute(delete(work_credits).where(work_credits.c.id == credit.id))


async def _remove_creator_credit_collisions(
    conn: AsyncConnection, loser_id: uuid.UUID, winner_id: uuid.UUID
) -> None:
    """Keep one logical source credit when merging duplicate creator identities."""
    loser_credits = await conn.execute(
        select(work_credits).where(work_credits.c.creator_id == loser_id)
    )
    for credit in loser_credits:
        collision = (
            await conn.execute(
                select(work_credits).where(
                    work_credits.c.work_id == credit.work_id,
                    work_credits.c.creator_id == winner_id,
                    work_credits.c.role == credit.role,
                    work_credits.c.source == credit.source,
                )
            )
        ).first()
        if collision is not None:
            _ensure_credit_override_compatible(credit, collision)
            await conn.execute(delete(work_credits).where(work_credits.c.id == credit.id))


def _ensure_credit_override_compatible(loser: Any, winner: Any) -> None:
    """Do not collapse a manual split onto a different source-credit key."""
    loser_origin = loser.manual_from_creator_id
    winner_origin = winner.manual_from_creator_id
    if (loser_origin is not None or winner_origin is not None) and (
        loser_origin != winner_origin
        or loser.position != winner.position
        or loser.role_raw != winner.role_raw
        or loser.credited_as != winner.credited_as
    ):
        raise ValueError("merge would discard a distinct manual creator split")
