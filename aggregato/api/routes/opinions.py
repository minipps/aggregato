"""``GET /opinions`` — ratings and reviews, paged newest-updated first ."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import ColumnElement, select
from starlette.requests import Request

from aggregato.api.pagination import clamp_limit
from aggregato.api.queries import aggregate_filters, fetch_page
from aggregato.api.schemas import Opinion, PageResponse, opinion_from_row
from aggregato.db.engine import transaction
from aggregato.db.schema import opinions

router = APIRouter(tags=["log"])


@router.get("/opinions", response_model=PageResponse[Opinion])
async def list_opinions(
    request: Request,
    provider: Annotated[list[str] | None, Query()] = None,
    score_min: int | None = None,
    score_max: int | None = None,
    has_review: bool | None = None,
    include_subunits: bool = False,
    limit: int | None = None,
    cursor: str | None = None,
) -> PageResponse[Opinion]:
    """Return one keyset page of undeleted opinions under the declared filters."""
    conditions: list[ColumnElement[bool]] = aggregate_filters(
        opinions.c.subject_ref,
        opinions.c.deleted_at,
        include_subunits=include_subunits,
        include_deleted=False,
    )
    if provider:
        conditions.append(opinions.c.provider_id.in_(provider))
    if score_min is not None:
        conditions.append(opinions.c.rating_normalized >= score_min)
    if score_max is not None:
        conditions.append(opinions.c.rating_normalized <= score_max)
    if has_review is not None:
        conditions.append(opinions.c.review_text.is_not(None) == has_review)

    async with transaction(request.app.state.engine) as conn:
        page = await fetch_page(
            conn,
            select(opinions).where(*conditions),
            sort_col=opinions.c.updated_at,
            id_col=opinions.c.id,
            sort="updated_at",
            order="desc",
            cursor=cursor,
            limit=clamp_limit(limit),
        )
    return PageResponse(
        items=[opinion_from_row(row) for row in page.items], next_cursor=page.next_cursor
    )
