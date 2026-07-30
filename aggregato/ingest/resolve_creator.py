"""Batched creator resolution: asserted identifiers, then family-scoped aliases (T072)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import creator_aliases, creator_external_ids, creators
from aggregato.db.upsert import upsert_stmt
from aggregato.domain.enums import AliasKind, Confidence, MediaFamily
from aggregato.domain.models import NormalizedBatch
from aggregato.ingest.titles import normalize_title


@dataclass(frozen=True, slots=True)
class CreatorResolution:
    creator_id: uuid.UUID
    confidence: Confidence


async def resolve_creators(
    conn: AsyncConnection,
    batch: NormalizedBatch,
    family: MediaFamily,
    *,
    source: str,
    now: datetime,
) -> list[CreatorResolution]:
    """Resolve all credits using two bounded lookups, then create unmatched creators.

    Asserted identifiers are universal. Names are only trusted within the work's media family;
    a same-spelled creator in another family is never silently joined.
    """
    ids_by_name: dict[str, list[tuple[str, str]]] = {}
    for identifier in batch.creator_external_ids:
        ids_by_name.setdefault(identifier.creator_name, []).append(
            (identifier.namespace, identifier.value)
        )
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
    for credit in batch.credits:
        asserted = set().union(
            *(id_matches.get(pair, set()) for pair in ids_by_name.get(credit.creator_name, []))
        )
        if len(asserted) == 1:
            resolution = CreatorResolution(asserted.pop(), Confidence.ASSERTED)
        else:
            by_name = alias_matches.get(normalize_title(credit.creator_name), set())
            if len(by_name) == 1:
                resolution = CreatorResolution(next(iter(by_name)), Confidence.MATCHED)
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
                resolution = CreatorResolution(creator_id, Confidence.ASSERTED)
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
                    constraint="uq_creator_external_ids_namespace_value_creator",
                    update_columns=None,
                )
            )
        resolved.append(resolution)
    return resolved
