"""Archive statistics .

The statistics surface deliberately consumes the same :func:`aggregate_filters` helper as every
other aggregate.  That keeps an episode or track from quietly becoming a whole-work count simply
because a new route chose the default again.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import ColumnElement, cast, func, select
from sqlalchemy.sql.sqltypes import String
from starlette.requests import Request

from aggregato.api.queries import aggregate_filters
from aggregato.db.engine import transaction
from aggregato.db.schema import creators, entries, work_credits, works
from aggregato.domain.enums import MediaType, Role

router = APIRouter(tags=["log"])

Period = Literal["all", "year", "month", "week", "day"]
TopGroup = Literal["work", "creator"]


class PeriodCount(BaseModel):
    """A labelled time bucket in the requested calendar granularity."""

    period: str
    count: int


class StatsSummary(BaseModel):
    """Counts from active archive entries, grouped for the Stats view."""

    period: Period
    total_entries: int
    by_media_type: dict[str, int]
    by_provider: dict[str, int]
    by_period: list[PeriodCount]


class TopItem(BaseModel):
    """One ranked work or creator, with only stable presentation data."""

    id: str
    label: str
    count: int
    media_type: MediaType | None = None


class TopResults(BaseModel):
    """All-time ranked archive activity for a work or creator grouping."""

    group: TopGroup
    items: list[TopItem]


def _entry_conditions(*, include_subunits: bool) -> list[ColumnElement[bool]]:
    """Return the canonical active-entry predicate for every stats query."""
    return aggregate_filters(
        entries.c.subject_ref,
        entries.c.deleted_at,
        include_subunits=include_subunits,
        include_deleted=False,
    )


def _bucket(period: Period, dialect_name: str) -> ColumnElement[str]:
    """Produce a portable, sortable calendar label for a timestamp.

    SQLite and Postgres expose different formatting functions, so the tiny dialect boundary stays
    here instead of leaking into every aggregate statement.  ``all`` is a literal single bucket.
    """
    if period == "all":
        return cast("all", String)
    sqlite_patterns = {"year": "%Y", "month": "%Y-%m", "week": "%Y-W%W", "day": "%Y-%m-%d"}
    postgres_patterns = {
        "year": "YYYY",
        "month": "YYYY-MM",
        "week": 'IYYY-"W"IW',
        "day": "YYYY-MM-DD",
    }
    if dialect_name == "postgresql":
        return func.to_char(entries.c.logged_at, postgres_patterns[period])
    return func.strftime(sqlite_patterns[period], entries.c.logged_at)


async def _counts(
    request: Request,
    column: ColumnElement[str],
    *,
    include_subunits: bool,
) -> dict[str, int]:
    """Count active entries grouped by one selected column."""
    async with transaction(request.app.state.engine) as conn:
        rows = await conn.execute(
            select(column.label("value"), func.count(entries.c.id).label("count"))
            .select_from(entries.join(works, entries.c.work_id == works.c.id))
            .where(*_entry_conditions(include_subunits=include_subunits))
            .group_by(column)
            .order_by(column)
        )
    return {str(row.value): int(row._mapping["count"]) for row in rows}


@router.get("/stats/summary", response_model=StatsSummary)
async def summary(
    request: Request,
    period: Period = "all",
    include_subunits: bool = False,
) -> StatsSummary:
    """Return active-entry counts by type, provider, and requested calendar granularity."""
    by_media_type = await _counts(request, works.c.media_type, include_subunits=include_subunits)
    by_provider = await _counts(request, entries.c.provider_id, include_subunits=include_subunits)
    async with transaction(request.app.state.engine) as conn:
        bucket = _bucket(period, conn.dialect.name)
        rows = await conn.execute(
            select(bucket.label("period"), func.count(entries.c.id).label("count"))
            .where(*_entry_conditions(include_subunits=include_subunits))
            .group_by(bucket)
            .order_by(bucket)
        )
    buckets = [
        PeriodCount(period=str(row.period), count=int(row._mapping["count"])) for row in rows
    ]
    return StatsSummary(
        period=period,
        total_entries=sum(item.count for item in buckets),
        by_media_type=by_media_type,
        by_provider=by_provider,
        by_period=buckets,
    )


@router.get("/stats/top", response_model=TopResults)
async def top(
    request: Request,
    group: TopGroup,
    role: Role | None = None,
    media_type: MediaType | None = None,
    include_subunits: bool = False,
) -> TopResults:
    """Rank works or creators by active logged entries.

    A creator's count is distinct on entry id: multiple credits on one work must not multiply one
    logged event into several statistics.
    """
    conditions = _entry_conditions(include_subunits=include_subunits)
    if media_type is not None:
        conditions.append(works.c.media_type == str(media_type))
    async with transaction(request.app.state.engine) as conn:
        if group == "work":
            rows = await conn.execute(
                select(
                    works.c.id,
                    works.c.title.label("label"),
                    works.c.media_type,
                    func.count(entries.c.id).label("count"),
                )
                .select_from(entries.join(works, entries.c.work_id == works.c.id))
                .where(*conditions)
                .group_by(works.c.id, works.c.title, works.c.media_type)
                .order_by(func.count(entries.c.id).desc(), works.c.title, works.c.id)
                .limit(10)
            )
            items = [
                TopItem(
                    id=str(row.id),
                    label=str(row.label),
                    count=int(row._mapping["count"]),
                    media_type=row.media_type,
                )
                for row in rows
            ]
        else:
            if role is not None:
                conditions.append(work_credits.c.role == str(role))
            rows = await conn.execute(
                select(
                    creators.c.id,
                    creators.c.name.label("label"),
                    func.count(func.distinct(entries.c.id)).label("count"),
                )
                .select_from(
                    entries.join(works, entries.c.work_id == works.c.id)
                    .join(work_credits, work_credits.c.work_id == works.c.id)
                    .join(creators, creators.c.id == work_credits.c.creator_id)
                )
                .where(*conditions)
                .group_by(creators.c.id, creators.c.name)
                .order_by(
                    func.count(func.distinct(entries.c.id)).desc(), creators.c.name, creators.c.id
                )
                .limit(10)
            )
            items = [
                TopItem(id=str(row.id), label=str(row.label), count=int(row._mapping["count"]))
                for row in rows
            ]
    return TopResults(group=group, items=items)
