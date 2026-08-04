"""Creator list and detail resources ."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import ColumnElement, and_, func, select
from starlette.requests import Request

from aggregato.api.errors import ProblemError
from aggregato.api.pagination import clamp_limit
from aggregato.api.queries import fetch_page
from aggregato.api.schemas import (
    Creator,
    CreatorAlias,
    CreatorDetail,
    Credit,
    ExternalId,
    PageResponse,
    credit_from_row,
)
from aggregato.db.engine import transaction
from aggregato.db.schema import (
    creator_aliases,
    creator_external_ids,
    creators,
    entries,
    work_credits,
)
from aggregato.domain.enums import CreatorKind, MediaFamily, Role

router = APIRouter(tags=["identity"])


@router.get("/creators", response_model=PageResponse[Creator])
async def list_creators(
    request: Request,
    kind: CreatorKind | None = None,
    role: Role | None = None,
    q: str | None = None,
    media_family: Annotated[list[MediaFamily] | None, Query()] = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> PageResponse[Creator]:
    """List creators with their credit and whole-work logged counts."""
    conditions: list[ColumnElement[bool]] = []
    if kind is not None:
        conditions.append(creators.c.kind == str(kind))
    if q:
        conditions.append(creators.c.sort_name.contains(q.casefold()))
    stmt = select(creators)
    if role is not None or media_family:
        stmt = stmt.join(work_credits, work_credits.c.creator_id == creators.c.id)
        if role is not None:
            conditions.append(work_credits.c.role == str(role))
        if media_family:
            stmt = stmt.join(creator_aliases, creator_aliases.c.creator_id == creators.c.id)
            conditions.append(
                creator_aliases.c.media_family.in_([str(value) for value in media_family])
            )
        stmt = stmt.distinct()
    async with transaction(request.app.state.engine) as conn:
        page = await fetch_page(
            conn,
            stmt.where(*conditions),
            sort_col=creators.c.created_at,
            id_col=creators.c.id,
            sort="created_at",
            order="desc",
            cursor=cursor,
            limit=clamp_limit(limit),
        )
        return PageResponse(
            items=[await _creator(conn, row) for row in page.items], next_cursor=page.next_cursor
        )


@router.get("/creators/{id}", response_model=CreatorDetail)
async def get_creator(request: Request, id: str) -> CreatorDetail:
    """Return aliases, asserted IDs, and credits grouped by their closed role vocabulary."""
    try:
        creator_id = uuid.UUID(id)
    except ValueError as exc:
        raise ProblemError(
            status=404, title="Not Found", detail=f"No creator with id {id!r}."
        ) from exc
    async with transaction(request.app.state.engine) as conn:
        row = (await conn.execute(select(creators).where(creators.c.id == creator_id))).first()
        if row is None:
            raise ProblemError(status=404, title="Not Found", detail=f"No creator with id {id!r}.")
        base = await _creator(conn, row)
        aliases = list(
            await conn.execute(
                select(creator_aliases).where(creator_aliases.c.creator_id == creator_id)
            )
        )
        ids = list(
            await conn.execute(
                select(creator_external_ids).where(creator_external_ids.c.creator_id == creator_id)
            )
        )
        credits = list(
            await conn.execute(
                select(work_credits, creators.c.name.label("creator_name"))
                .select_from(work_credits.join(creators))
                .where(work_credits.c.creator_id == creator_id)
            )
        )
    grouped: dict[str, list[Credit]] = defaultdict(list)
    for credit in credits:
        grouped[credit.role].append(credit_from_row(credit))
    return CreatorDetail(
        **base.model_dump(),
        aliases=[
            CreatorAlias(name=row.name, media_family=MediaFamily(row.media_family), kind=row.kind)
            for row in aliases
        ],
        external_ids=[
            ExternalId(
                namespace=row.namespace,
                value=row.value,
                source=row.source,
                confidence=row.confidence,
            )
            for row in ids
        ],
        credits_by_role=dict(grouped),
    )


async def _creator(conn: Any, row: Any) -> Creator:
    credit_count = int(
        (
            await conn.execute(
                select(func.count())
                .select_from(work_credits)
                .where(work_credits.c.creator_id == row.id)
            )
        ).scalar_one()
    )
    logged_count = int(
        (
            await conn.execute(
                select(func.count())
                .select_from(
                    entries.join(work_credits, entries.c.work_id == work_credits.c.work_id)
                )
                .where(
                    and_(
                        work_credits.c.creator_id == row.id,
                        entries.c.deleted_at.is_(None),
                        entries.c.subject_ref.is_(None),
                    )
                )
            )
        ).scalar_one()
    )
    families = list(
        (
            await conn.execute(
                select(creator_aliases.c.media_family)
                .where(creator_aliases.c.creator_id == row.id)
                .distinct()
            )
        ).scalars()
    )
    return Creator(
        id=row.id,
        kind=CreatorKind(row.kind),
        name=row.name,
        image=None,
        families=[MediaFamily(family) for family in families],
        credit_count=credit_count,
        logged_count=logged_count,
    )
