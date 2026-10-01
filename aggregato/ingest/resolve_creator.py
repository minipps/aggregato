"""Batched creator resolution: asserted identifiers, aliases, and scoped manual splits."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import (
    creator_aliases,
    creator_external_ids,
    creators,
    merge_log,
    work_credits,
    works,
)
from aggregato.db.upsert import upsert_stmt
from aggregato.domain.enums import AliasKind, Confidence, MediaFamily
from aggregato.domain.models import NormalizedBatch
from aggregato.ingest.titles import normalize_title


@dataclass(frozen=True, slots=True)
class CreatorResolution:
    creator_id: uuid.UUID
    confidence: Confidence


class CreatorIdentityConflict(ValueError):
    """An asserted provider identifier is attached to more than one creator.

    Continuing by creating another creator would turn one corrupt identity into an ever-growing
    cluster.  The writer records this as an ingest failure so an operator can repair the archive.
    """


CreatorResolutionMemo = MutableMapping[
    tuple[str, str, tuple[tuple[str, str], ...]], CreatorResolution
]
"""Sync-scoped cache keyed by family, normalized credit name, and asserted identifiers."""


async def resolve_creators(
    conn: AsyncConnection,
    batch: NormalizedBatch,
    family: MediaFamily,
    *,
    source: str,
    now: datetime,
    work_id: uuid.UUID | None = None,
    memo: CreatorResolutionMemo | None = None,
) -> list[CreatorResolution]:
    """Resolve asserted IDs and family-scoped aliases, honoring matching manual splits.

    Asserted identifiers are universal. Names are only trusted within the work's media family;
    a same-spelled creator in another family is never silently joined.
    """
    ids_by_name: dict[str, list[tuple[str, str]]] = {}
    for identifier in batch.creator_external_ids:
        ids_by_name.setdefault(identifier.creator_name, []).append(
            (identifier.namespace, identifier.value)
        )
    keys = [
        _memo_key(family, credit.creator_name, ids_by_name.get(credit.creator_name, []))
        for credit in batch.credits
    ]
    manual_by_key: dict[tuple[int, str | None, str | None], list[tuple[uuid.UUID, uuid.UUID]]] = {}
    if work_id is not None and batch.credits:
        rows = await conn.execute(
            select(
                work_credits.c.creator_id,
                work_credits.c.manual_from_creator_id,
                work_credits.c.position,
                work_credits.c.role_raw,
                work_credits.c.credited_as,
            )
            .select_from(
                works.outerjoin(
                    work_credits,
                    and_(
                        work_credits.c.work_id == works.c.id,
                        work_credits.c.source == source,
                        work_credits.c.position.in_({credit.position for credit in batch.credits}),
                        work_credits.c.link_confidence == str(Confidence.MANUAL),
                        work_credits.c.manual_from_creator_id.is_not(None),
                    ),
                )
            )
            .where(works.c.id == work_id)
            .with_for_update(of=works)
        )
        for row in rows:
            if row.creator_id is None:
                continue
            assert row.manual_from_creator_id is not None
            manual_by_key.setdefault((row.position, row.role_raw, row.credited_as), []).append(
                (row.creator_id, row.manual_from_creator_id)
            )
    has_manual_match = any(
        manual_by_key.get((credit.position, credit.role_raw, credit.credited_as))
        for credit in batch.credits
    )
    successors: dict[uuid.UUID, uuid.UUID] = {}
    if has_manual_match:
        # ponytail: This scans active merge links only for a record with a matching manual credit;
        # if merge history grows enough to matter, index and fetch only each referenced origin.
        rows = await conn.execute(
            select(merge_log.c.winner_id, merge_log.c.loser_ids).where(
                merge_log.c.subject == "creator",
                merge_log.c.operation == "merge",
                merge_log.c.undone_at.is_(None),
            )
        )
        for row in rows:
            for loser_id in row.loser_ids or []:
                successors[uuid.UUID(str(loser_id))] = row.winner_id
    if memo is not None and not has_manual_match:
        cached = [memo.get(key) for key in keys]
        if all(resolution is not None for resolution in cached):
            return [resolution for resolution in cached if resolution is not None]
    identifiers = [pair for pairs in ids_by_name.values() for pair in pairs]
    id_matches: dict[tuple[str, str], set[uuid.UUID]] = {}
    if identifiers:
        rows = await conn.execute(
            select(
                creator_external_ids.c.creator_id,
                creator_external_ids.c.namespace,
                creator_external_ids.c.value,
            ).where(
                or_(
                    *[
                        and_(
                            creator_external_ids.c.namespace == namespace,
                            creator_external_ids.c.value == value,
                        )
                        for namespace, value in identifiers
                    ]
                )
            )
        )
        for row in rows:
            id_matches.setdefault((row.namespace, row.value), set()).add(row.creator_id)
    names = {normalize_title(credit.creator_name) for credit in batch.credits}
    alias_matches: dict[str, set[uuid.UUID]] = {}
    if names:
        rows = await conn.execute(
            select(creator_aliases.c.normalized, creator_aliases.c.creator_id).where(
                creator_aliases.c.media_family == str(family),
                creator_aliases.c.normalized.in_(names),
            )
        )
        for row in rows:
            alias_matches.setdefault(row.normalized, set()).add(row.creator_id)

    resolved: list[CreatorResolution] = []
    for credit, key in zip(batch.credits, keys, strict=True):
        automatic = memo.get(key) if memo is not None else None
        if automatic is None:
            asserted = set().union(
                *(id_matches.get(pair, set()) for pair in ids_by_name.get(credit.creator_name, []))
            )
            if len(asserted) == 1:
                automatic = CreatorResolution(asserted.pop(), Confidence.ASSERTED)
            elif asserted:
                raise CreatorIdentityConflict(
                    "asserted creator identifier belongs to multiple creators: "
                    + ", ".join(str(value) for value in sorted(asserted, key=str))
                )
            else:
                by_name = alias_matches.get(normalize_title(credit.creator_name), set())
                if len(by_name) == 1:
                    automatic = CreatorResolution(next(iter(by_name)), Confidence.MATCHED)
                else:
                    creator_id = uuid.uuid4()
                    await conn.execute(
                        creators.insert().values(
                            id=creator_id,
                            kind=str(credit.creator_kind),
                            name=credit.creator_name,
                            sort_name=normalize_title(credit.creator_name),
                            metadata={},
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await conn.execute(
                        creator_aliases.insert().values(
                            creator_id=creator_id,
                            name=credit.creator_name,
                            normalized=normalize_title(credit.creator_name),
                            media_family=str(family),
                            kind=str(AliasKind.PRIMARY),
                            source=source,
                        )
                    )
                    automatic = CreatorResolution(creator_id, Confidence.ASSERTED)
                    # Later credits in this batch must see what was just created.  The original
                    # batched query intentionally precedes all inserts, so without these local
                    # updates a repeated credit creates a duplicate before the sync-scoped memo is
                    # populated.
                    alias_matches.setdefault(normalize_title(credit.creator_name), set()).add(
                        creator_id
                    )
                    for pair in ids_by_name.get(credit.creator_name, []):
                        id_matches.setdefault(pair, set()).add(creator_id)

        manual_targets = {
            target_id
            for target_id, source_id in manual_by_key.get(
                (credit.position, credit.role_raw, credit.credited_as), []
            )
            if _follow_creator_merges(source_id, successors)
            == _follow_creator_merges(automatic.creator_id, successors)
        }
        if len(manual_targets) > 1:
            raise CreatorIdentityConflict(
                "manual split maps one source credit to multiple creators"
            )
        if manual_targets:
            resolved.append(CreatorResolution(next(iter(manual_targets)), Confidence.MANUAL))
            continue
        if memo is not None and key in memo:
            resolved.append(automatic)
            continue
        resolution = automatic
        for namespace, value in ids_by_name.get(credit.creator_name, []):
            await conn.execute(
                upsert_stmt(
                    conn,
                    creator_external_ids,
                    [
                        {
                            "creator_id": resolution.creator_id,
                            "namespace": namespace,
                            "value": value,
                            "source": source,
                            "confidence": str(resolution.confidence),
                        }
                    ],
                    constraint="uq_creator_external_ids_namespace_value",
                    update_columns=None,
                )
            )
        resolved.append(resolution)
        if memo is not None:
            memo[key] = resolution
    return resolved


def _memo_key(
    family: MediaFamily, creator_name: str, identifiers: list[tuple[str, str]]
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """Keep identifier-backed and name-only results separate in the sync-scoped memo."""
    return (str(family), normalize_title(creator_name), tuple(sorted(identifiers)))


def _follow_creator_merges(
    creator_id: uuid.UUID, successors: Mapping[uuid.UUID, uuid.UUID]
) -> uuid.UUID:
    """Resolve historical creator IDs through active merge links, failing closed on cycles."""
    seen: set[uuid.UUID] = set()
    while creator_id in successors:
        if creator_id in seen:
            # Corrupt cyclic history must not make an unrelated creator look identical.
            return creator_id
        seen.add(creator_id)
        creator_id = successors[creator_id]
    return creator_id
