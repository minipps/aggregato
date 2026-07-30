"""``GET /entries`` — the primary log feed (T053).

The most heavily filtered read in the system, and the one whose defaults matter most:

* ``include_subunits`` defaults **false**. A per-episode logger's feed shows seasons, not 300
  episodes, unless asked (FR-007, research.md R18).
* ``include_deleted`` defaults **false**. Tombstoned rows exist forever but are not the log
  (FR-024).
* Paging is keyset-only. There is no ``offset`` parameter here or anywhere else (FR-030).
* ``logged_at`` always travels with ``logged_precision``, so a client cannot invent an exact time
  (FR-004).

An entry has no score column of its own — a score belongs to an opinion — so ``score_min``,
``score_max``, ``has_review`` and ``sort=score`` all read the opinion that came from the same
platform record (``provider_item_id``). See :func:`~aggregato.api.queries.opinion_facts`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Query
from sqlalchemy import ColumnElement, func, or_, select
from starlette.requests import Request

from aggregato.api.pagination import SortKey, SortOrder, clamp_limit
from aggregato.api.queries import (
    WorkAggregate,
    aggregate_filters,
    aware,
    credit_filter,
    fetch_page,
    media_type_values,
    opinion_facts,
    review_matches,
    title_matches,
    work_aggregates,
    work_rows,
)
from aggregato.api.schemas import Entry, PageResponse, entry_from_row, work_from_row
from aggregato.db.engine import transaction
from aggregato.db.schema import entries, works
from aggregato.domain.enums import COMPLETED_KINDS, EntryKind, MediaFamily, MediaType, Role

router = APIRouter(tags=["log"])


@router.get("/entries", response_model=PageResponse[Entry])
async def list_entries(
    request: Request,
    media_type: Annotated[list[MediaType] | None, Query()] = None,
    media_family: Annotated[list[MediaFamily] | None, Query()] = None,
    provider: Annotated[list[str] | None, Query()] = None,
    kind: EntryKind | None = None,
    status: Literal["completed"] | None = None,
    creator: uuid.UUID | None = None,
    role: Role | None = None,
    # ``from`` is a Python keyword, so the contract's name lives in the alias.
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    score_min: Annotated[int | None, Query(ge=0, le=100)] = None,
    score_max: Annotated[int | None, Query(ge=0, le=100)] = None,
    has_review: bool | None = None,
    include_subunits: bool = False,
    include_deleted: bool = False,
    q: str | None = None,
    sort: SortKey = "logged_at",
    order: SortOrder = "desc",
    limit: int | None = None,
    cursor: str | None = None,
) -> PageResponse[Entry]:
    """One keyset page of the log, newest first by default.

    Inputs: every filter the contract declares. ``media_family`` expands to its member media types
    (FR-029); repeated ``media_type``/``media_family``/``provider`` values are ORed within a
    parameter and ANDed across parameters. ``status=completed`` expands the same way over
    ``kind`` (``COMPLETED_KINDS``), and ANDs with ``kind`` if both are given. ``q`` searches
    indexed work titles and review text (FR-028).

    Failure modes: 400 problem+json for a cursor this API did not issue — never a silent restart;
    422 problem+json for a value outside a closed vocabulary; 401 without credentials.
    """
    facts = opinion_facts(include_deleted=include_deleted)
    conditions: list[ColumnElement[bool]] = aggregate_filters(
        entries.c.subject_ref,
        entries.c.deleted_at,
        include_subunits=include_subunits,
        include_deleted=include_deleted,
    )

    types = media_type_values(media_type, media_family)
    if types is not None:
        # A subquery rather than a join: nothing from ``works`` is selected here, and the entry's
        # work is loaded once per page below.
        conditions.append(
            entries.c.work_id.in_(select(works.c.id).where(works.c.media_type.in_(types)))
        )
    if provider:
        conditions.append(entries.c.provider_id.in_(provider))
    if kind is not None:
        conditions.append(entries.c.kind == str(kind))
    if status == "completed":
        # Possible refactor while the API is still unstable: make ``kind`` a repeated parameter
        # like ``media_type``/``provider`` and drop ``status`` entirely — clients would then send
        # the four (five, with ``rewatch``) kinds themselves. It removes a parameter and a
        # server-side vocabulary, at the price of moving the definition of "completed" into every
        # client, each of which re-decides whether ``rewatch`` belongs. Keep ``status`` if that
        # definition should stay one thing; drop it if callers want arbitrary kind sets more.
        conditions.append(entries.c.kind.in_([str(value) for value in COMPLETED_KINDS]))
    credited = credit_filter(entries.c.work_id, creator, role)
    if credited is not None:
        conditions.append(credited)
    if from_ is not None:
        conditions.append(entries.c.logged_at >= _utc(from_))
    if to is not None:
        conditions.append(entries.c.logged_at <= _utc(to))
    if score_min is not None:
        conditions.append(facts.c.score >= score_min)
    if score_max is not None:
        conditions.append(facts.c.score <= score_max)
    if has_review is not None:
        # COALESCE, because the outer join leaves ``reviewed`` null for an entry with no opinion at
        # all — which is "no review", not "unknown".
        conditions.append(func.coalesce(facts.c.reviewed, 0) == int(has_review))

    async with transaction(request.app.state.engine) as conn:
        if q:
            titles = await title_matches(conn, q)
            reviews = await review_matches(conn, q)
            conditions.append(
                or_(entries.c.work_id.in_(titles), entries.c.provider_item_id.in_(reviews))
            )

        statement = (
            select(entries)
            .select_from(
                entries.outerjoin(facts, facts.c.provider_item_id == entries.c.provider_item_id)
            )
            .where(*conditions)
        )
        page = await fetch_page(
            conn,
            statement,
            sort_col=_sort_column(sort, facts),
            id_col=entries.c.id,
            sort=sort,
            order=order,
            cursor=cursor,
            limit=clamp_limit(limit),
        )

        work_ids = [row.work_id for row in page.items]
        rows = await work_rows(conn, work_ids)
        aggregates = await work_aggregates(
            conn,
            work_ids,
            include_subunits=include_subunits,
            include_deleted=include_deleted,
        )

    embedded = {
        work_id: work_from_row(row, aggregates.get(work_id, WorkAggregate()))
        for work_id, row in rows.items()
    }
    return PageResponse(
        items=[entry_from_row(row, embedded.get(row.work_id)) for row in page.items],
        next_cursor=page.next_cursor,
    )


def _sort_column(sort: SortKey, facts: Any) -> ColumnElement[Any]:
    """The column a ``sort`` value names; ``score`` lives on the joined opinion."""
    if sort == "ingested_at":
        return entries.c.ingested_at
    if sort == "score":
        return cast("ColumnElement[Any]", facts.c.score)
    return entries.c.logged_at


def _utc(value: datetime) -> datetime:
    """Read a boundary timestamp as UTC when the client omitted the offset.

    Rejecting it would be defensible, but every stored timestamp is UTC and assuming so is what the
    operator meant; the alternative is a 422 for ``from=2026-01-01``.
    """
    coerced = aware(value)
    assert coerced is not None
    return coerced
