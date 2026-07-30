"""``GET /works`` and ``GET /works/{id}`` (T054).

``media_family`` is derived from ``media_type`` on the way out and never stored (data-model.md §1).
The two can never disagree. ``entry_count`` and ``providers`` are batched per page, not per row.

``/works`` has no ``sort`` parameter, so it pages by ``(created_at desc, id desc)`` — a total order
with the same keyset guarantees as the feed (R6). ``/works/{id}`` answers with the parent and the
sibling seasons as well, which is what lets a client walk the hierarchy without a request per hop.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query
from sqlalchemy import ColumnElement, Row, and_, exists, select
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request

from aggregato.api.errors import ProblemError
from aggregato.api.pagination import LIMIT_MAX, clamp_limit
from aggregato.api.queries import (
    WorkAggregate,
    aggregate_filters,
    credit_filter,
    credits_for,
    fetch_page,
    media_type_values,
    provider_filter,
    title_matches,
    work_aggregates,
)
from aggregato.api.schemas import (
    PageResponse,
    Work,
    WorkDetail,
    credit_from_row,
    entry_from_row,
    external_id_from_row,
    opinion_from_row,
    work_from_row,
)
from aggregato.db.engine import transaction
from aggregato.db.schema import entries, external_ids, opinions, works
from aggregato.domain.enums import MediaFamily, MediaType

router = APIRouter(tags=["log"])

_NOT_FOUND: Final = 404

#: Cap on the entries and opinions embedded in a work detail response.
#:
#: ponytail: a rewatcher's favourite film can hold hundreds of entries, and this returns the most
#: recent 200 rather than paging. The contract has no ``work`` filter on ``/entries``, so this is a
#: documented ceiling rather than an invented
#: parameter.
_EMBEDDED_LIMIT: Final = LIMIT_MAX


@router.get("/works", response_model=PageResponse[Work])
async def list_works(
    request: Request,
    media_type: Annotated[list[MediaType] | None, Query()] = None,
    media_family: Annotated[list[MediaFamily] | None, Query()] = None,
    q: str | None = None,
    year: int | None = None,
    provider: Annotated[list[str] | None, Query()] = None,
    creator: uuid.UUID | None = None,
    has_opinion: bool | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> PageResponse[Work]:
    """One keyset page of works, most recently added first.

    Inputs: the contract's filters. ``media_family`` expands to its member media types (FR-029);
    ``provider`` matches any provider holding an item for the work, so a work known only through a
    rating still matches; ``q`` searches indexed titles, including original-language forms (FR-028).

    Failure modes: 400 problem+json for a cursor this API did not issue; 401 without credentials.
    """
    conditions: list[ColumnElement[bool]] = []
    types = media_type_values(media_type, media_family)
    if types is not None:
        conditions.append(works.c.media_type.in_(types))
    if year is not None:
        conditions.append(works.c.release_year == year)
    provided = provider_filter(works.c.id, provider)
    if provided is not None:
        conditions.append(provided)
    credited = credit_filter(works.c.id, creator, None)
    if credited is not None:
        conditions.append(credited)
    if has_opinion is not None:
        opinionated = exists(
            select(opinions.c.id).where(
                and_(opinions.c.work_id == works.c.id, opinions.c.deleted_at.is_(None))
            )
        )
        conditions.append(opinionated if has_opinion else ~opinionated)

    async with transaction(request.app.state.engine) as conn:
        if q:
            conditions.append(works.c.id.in_(await title_matches(conn, q)))
        page = await fetch_page(
            conn,
            select(works).where(*conditions),
            sort_col=works.c.created_at,
            id_col=works.c.id,
            sort="created_at",
            order="desc",
            cursor=cursor,
            limit=clamp_limit(limit),
        )
        aggregates = await work_aggregates(conn, [row.id for row in page.items])

    return PageResponse(
        items=[work_from_row(row, aggregates.get(row.id, WorkAggregate())) for row in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/works/{id}", response_model=WorkDetail)
async def get_work(
    request: Request,
    # ``id`` shadows the builtin because the contract names the path parameter that.
    id: str,
    include_subunits: bool = False,
) -> WorkDetail:
    """One work with its identifiers, credits, entries, opinions, parent and sibling seasons.

    Inputs: the work id, and ``include_subunits`` — false by default, so the entries and opinions
    listed are the ones about the work itself rather than about its episodes (FR-007, R18).

    Failure modes: 404 problem+json for an unknown or malformed UUID. Tombstoned entries and
    opinions are excluded (FR-024).
    """
    try:
        work_id = uuid.UUID(id)
    except ValueError as exc:
        raise _missing(id) from exc

    async with transaction(request.app.state.engine) as conn:
        row = (await conn.execute(select(works).where(works.c.id == work_id))).first()
        if row is None:
            raise _missing(id)

        parent_row = None
        if row.parent_work_id is not None:
            parent_row = (
                await conn.execute(select(works).where(works.c.id == row.parent_work_id))
            ).first()

        sibling_rows = []
        if row.parent_work_id is not None:
            sibling_rows = list(
                await conn.execute(
                    select(works)
                    .where(
                        works.c.parent_work_id == row.parent_work_id,
                        works.c.id != work_id,
                    )
                    .order_by(works.c.sequence_number, works.c.sort_title)
                )
            )

        related = [work_id, *(r.id for r in sibling_rows)]
        if parent_row is not None:
            related.append(parent_row.id)
        aggregates = await work_aggregates(conn, related, include_subunits=include_subunits)

        identifiers = list(
            await conn.execute(
                select(external_ids)
                .where(external_ids.c.work_id == work_id)
                .order_by(external_ids.c.namespace, external_ids.c.value)
            )
        )
        credits = (await credits_for(conn, [work_id])).get(work_id, [])
        entry_rows, opinion_rows = await _records_for(
            conn, work_id, include_subunits=include_subunits
        )

    aggregate = aggregates.get(work_id, WorkAggregate())
    base = work_from_row(row, aggregate)
    return WorkDetail(
        **base.model_dump(),
        external_ids=[external_id_from_row(r) for r in identifiers],
        credits=[credit_from_row(r) for r in credits],
        entries=[entry_from_row(r) for r in entry_rows],
        opinions=[opinion_from_row(r) for r in opinion_rows],
        parent=(
            None
            if parent_row is None
            else work_from_row(parent_row, aggregates.get(parent_row.id, WorkAggregate()))
        ),
        siblings=[work_from_row(r, aggregates.get(r.id, WorkAggregate())) for r in sibling_rows],
    )


async def _records_for(
    conn: AsyncConnection, work_id: uuid.UUID, *, include_subunits: bool
) -> tuple[list[Row[Any]], list[Row[Any]]]:
    """The work's newest entries and opinions under shared sub-unit and tombstone rules."""
    entry_rows = list(
        await conn.execute(
            select(entries)
            .where(
                entries.c.work_id == work_id,
                *aggregate_filters(
                    entries.c.subject_ref,
                    entries.c.deleted_at,
                    include_subunits=include_subunits,
                    include_deleted=False,
                ),
            )
            .order_by(entries.c.logged_at.desc(), entries.c.id.desc())
            .limit(_EMBEDDED_LIMIT)
        )
    )
    opinion_rows = list(
        await conn.execute(
            select(opinions)
            .where(
                opinions.c.work_id == work_id,
                *aggregate_filters(
                    opinions.c.subject_ref,
                    opinions.c.deleted_at,
                    include_subunits=include_subunits,
                    include_deleted=False,
                ),
            )
            .order_by(opinions.c.updated_at.desc(), opinions.c.id.desc())
            .limit(_EMBEDDED_LIMIT)
        )
    )
    return entry_rows, opinion_rows


def _missing(id: str) -> ProblemError:
    return ProblemError(status=_NOT_FOUND, title="Not Found", detail=f"No work with id {id!r}.")
