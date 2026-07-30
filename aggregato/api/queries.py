"""The read path's shared query pieces (T053–T055).

Three endpoints read the log, and two of their filters are defaults that must not be re-decided per
endpoint:

* ``include_subunits`` defaults **false** — a record with a non-null ``subject_ref`` is a sub-unit
  (an episode, a track) and is excluded from work-level results unless asked for (FR-007,
  research.md R18). Getting the direction backwards corrupts every statistic a per-episode logger
  sees, silently, so the direction is decided once in :func:`aggregate_filters`.
* ``include_deleted`` defaults **false** — tombstoned rows are excluded (FR-024). They are never
  hard-deleted, so the filter is the only thing standing between "deleted" and "still there".

Everything else here exists to keep the routes free of N+1 queries: :func:`work_aggregates` and
:func:`credits_for` take a whole page of ids and answer in one statement each.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, Row, Select, and_, case, exists, func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.api.errors import ProblemError, error_type
from aggregato.api.pagination import (
    Cursor,
    CursorKey,
    CursorValue,
    InvalidCursor,
    Page,
    SortOrder,
    decode_cursor,
    encode_cursor,
    keyset_where,
)
from aggregato.db.schema import creators, entries, opinions, provider_items, work_credits, works
from aggregato.db.search import SearchKind, matching_ref_ids
from aggregato.domain.enums import MediaFamily, MediaType, Role
from aggregato.domain.families import types_in_families

_BAD_REQUEST: Final = 400

# Labels the keyset machinery adds to every paged statement. Named rather than positional so a route
# can select whatever columns it likes without knowing where these land.
_CURSOR_SORT: Final = "cursor_sort"
_CURSOR_ID: Final = "cursor_id"


def aggregate_filters(
    subject_ref_col: ColumnElement[Any],
    deleted_at_col: ColumnElement[Any],
    *,
    include_subunits: bool,
    include_deleted: bool,
) -> list[ColumnElement[bool]]:
    """The two defaults every log query shares, in the one place they are decided.

    Args:
        subject_ref_col: The table's ``subject_ref`` column.
        deleted_at_col: The table's ``deleted_at`` column.
        include_subunits: When false — the default — sub-unit records are excluded (FR-007, R18).
        include_deleted: When false — the default — tombstoned rows are excluded (FR-024).

    Returns:
        Conditions to AND into the query, possibly empty when the caller asked for everything.
    """
    conditions: list[ColumnElement[bool]] = []
    if not include_subunits:
        conditions.append(subject_ref_col.is_(None))
    if not include_deleted:
        conditions.append(deleted_at_col.is_(None))
    return conditions


def media_type_values(
    media_type: Sequence[MediaType] | None,
    media_family: Sequence[MediaFamily] | None,
) -> list[str] | None:
    """Resolve the ``media_type`` and ``media_family`` parameters to one list of type values.

    A ``media_family`` expands to its member types (FR-029) so individual types stay separately
    addressable. Passing both is the union of the two, which is the only reading under which each
    parameter still means what it says on its own.

    Returns:
        The type values to filter on, or ``None`` when neither parameter was given.
    """
    if not media_type and not media_family:
        return None
    selected: set[MediaType] = set(media_type or ())
    selected |= types_in_families(media_family or ())
    return [str(value) for value in sorted(selected)]


def credit_filter(
    work_id_col: ColumnElement[Any],
    creator: uuid.UUID | None,
    role: Role | None,
) -> ColumnElement[bool] | None:
    """Restrict to works credited to ``creator``, in ``role``, or both.

    An ``EXISTS`` rather than a join, because a work with three matching credits must still appear
    once — a join would multiply the row.

    Returns:
        The condition, or ``None`` when neither parameter was given.
    """
    if creator is None and role is None:
        return None
    conditions = [work_credits.c.work_id == work_id_col]
    if creator is not None:
        conditions.append(work_credits.c.creator_id == creator)
    if role is not None:
        conditions.append(work_credits.c.role == str(role))
    return exists(select(work_credits.c.id).where(and_(*conditions)))


def provider_filter(
    work_id_col: ColumnElement[Any], provider: Sequence[str] | None
) -> ColumnElement[bool] | None:
    """Restrict to works some listed provider has an item for.

    ``provider_items`` rather than ``entries``, so a work known only through a rating still matches
    the platform that supplied it.
    """
    if not provider:
        return None
    return exists(
        select(provider_items.c.id).where(
            and_(
                provider_items.c.work_id == work_id_col,
                provider_items.c.provider_id.in_(list(provider)),
            )
        )
    )


def aware(value: datetime | None) -> datetime | None:
    """Stamp UTC on a timestamp SQLite handed back naive.

    Every stored timestamp is UTC (db/types.py), but SQLite has no timestamp type and drops the
    offset, while the contract says timestamps carry one. This is that boundary.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def fetch_page(
    conn: AsyncConnection,
    statement: Select[Any],
    *,
    sort_col: ColumnElement[Any],
    id_col: ColumnElement[Any],
    sort: CursorKey,
    order: SortOrder,
    cursor: str | None,
    limit: int,
) -> Page[Row[Any]]:
    """Run one keyset page of ``statement`` and mint the cursor for the next.

    Ordering is ``(sort_col nulls last, id_col)`` in the requested direction, matching the null
    convention :func:`~aggregato.api.pagination.keyset_where` builds against. ``limit + 1`` rows are
    read: the extra row is what distinguishes "more to come" from "exactly a full last page", so
    ``next_cursor`` is never a cursor onto nothing.

    Args:
        conn: Connection to read on.
        statement: The filtered select, without ORDER BY or LIMIT.
        sort_col: The column named by the endpoint's sort key.
        id_col: The primary key, the tiebreaker that makes the order total (R6).
        sort: The cursor key, carried in the cursor so it cannot be replayed elsewhere.
        order: ``asc`` or ``desc``.
        cursor: The client's ``cursor`` parameter, or ``None`` for the first page.
        limit: Already-clamped page size.

    Returns:
        A :class:`~aggregato.api.pagination.Page` of raw rows.

    Raises:
        ProblemError: 400 when ``cursor`` is not a cursor this API issued. Never a silent restart
            from the beginning, which would re-serve page one forever (FR-030).
    """
    direction: Callable[[ColumnElement[Any]], ColumnElement[Any]] = (
        (lambda column: column.asc()) if order == "asc" else (lambda column: column.desc())
    )
    try:
        if cursor is not None:
            statement = statement.where(
                keyset_where(sort_col, id_col, order, decode_cursor(cursor, sort))
            )
    except InvalidCursor as exc:
        raise ProblemError(
            status=_BAD_REQUEST,
            title="Bad Request",
            detail=f"{exc} Request the first page without a cursor.",
            type=error_type("invalid-cursor"),
        ) from exc

    statement = (
        statement.add_columns(sort_col.label(_CURSOR_SORT), id_col.label(_CURSOR_ID))
        .order_by(nulls_last(direction(sort_col)), direction(id_col))
        .limit(limit + 1)
    )
    rows = (await conn.execute(statement)).all()
    window = list(rows[:limit])
    next_cursor: str | None = None
    if len(rows) > limit and window:
        last = window[-1]._mapping
        next_cursor = encode_cursor(
            Cursor(sort=sort, value=_cursor_value(last[_CURSOR_SORT]), id=str(last[_CURSOR_ID]))
        )
    return Page(items=window, next_cursor=next_cursor)


def _cursor_value(value: Any) -> CursorValue:
    """Coerce a sort column's value into what the cursor codec accepts."""
    if isinstance(value, datetime):
        return aware(value)
    if value is None or isinstance(value, int):
        return value
    raise TypeError(f"sort column returned {type(value).__name__}, which no cursor can carry")


@dataclass(frozen=True, slots=True)
class WorkAggregate:
    """The per-work counts the ``Work`` schema carries, computed for a whole page at once.

    Attributes:
        entry_count: Entries against the work, under the caller's sub-unit and tombstone defaults.
        providers: Every provider that has an item for this work, sorted.
    """

    entry_count: int = 0
    providers: list[str] = field(default_factory=list)


async def work_aggregates(
    conn: AsyncConnection,
    work_ids: Sequence[uuid.UUID],
    *,
    include_subunits: bool = False,
    include_deleted: bool = False,
) -> dict[uuid.UUID, WorkAggregate]:
    """``entry_count`` and ``providers`` for every work on a page, in two statements.

    ``entry_count`` is a work-level aggregate, so it obeys the sub-unit default (R18): by default an
    18-episode season counts as the entries logged against the season itself, not against episodes.

    Args:
        conn: Connection to read on.
        work_ids: The page's work ids. An empty sequence issues no query.
        include_subunits: Whether sub-unit entries count toward ``entry_count``.
        include_deleted: Whether tombstoned entries count.

    Returns:
        Aggregates by work id. Works with no entries and no items are absent; callers substitute an
        empty :class:`WorkAggregate` rather than treating the gap as an error.
    """
    ids = list(dict.fromkeys(work_ids))
    if not ids:
        return {}

    counts = await conn.execute(
        select(entries.c.work_id, func.count().label("n"))
        .where(
            entries.c.work_id.in_(ids),
            *aggregate_filters(
                entries.c.subject_ref,
                entries.c.deleted_at,
                include_subunits=include_subunits,
                include_deleted=include_deleted,
            ),
        )
        .group_by(entries.c.work_id)
    )
    by_work = {row.work_id: int(row.n) for row in counts}

    items = await conn.execute(
        select(provider_items.c.work_id, provider_items.c.provider_id)
        .where(provider_items.c.work_id.in_(ids))
        .distinct()
        .order_by(provider_items.c.provider_id)
    )
    providers: dict[uuid.UUID, list[str]] = {}
    for row in items:
        providers.setdefault(row.work_id, []).append(row.provider_id)

    return {
        work_id: WorkAggregate(
            entry_count=by_work.get(work_id, 0), providers=providers.get(work_id, [])
        )
        for work_id in set(by_work) | set(providers)
    }


async def work_rows(
    conn: AsyncConnection, work_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Row[Any]]:
    """Every listed work, in one statement, keyed by id.

    This is what keeps an embedded work off the N+1 path: a page of 200 entries fetches its works
    once, not once per row.
    """
    ids = list(dict.fromkeys(work_ids))
    if not ids:
        return {}
    rows = await conn.execute(select(works).where(works.c.id.in_(ids)))
    return {row.id: row for row in rows}


async def credits_for(
    conn: AsyncConnection, work_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Row[Any]]]:
    """Credits with their creator's name, for several works in one statement.

    Ordered by ``position``, which is billing order where the platform expressed one (FR-016).
    """
    ids = list(dict.fromkeys(work_ids))
    if not ids:
        return {}
    rows = await conn.execute(
        select(
            work_credits.c.id,
            work_credits.c.work_id,
            work_credits.c.creator_id,
            creators.c.name.label("creator_name"),
            work_credits.c.role,
            work_credits.c.role_raw,
            work_credits.c.credited_as,
            work_credits.c.position,
            work_credits.c.source,
            work_credits.c.link_confidence,
        )
        .select_from(work_credits.join(creators, creators.c.id == work_credits.c.creator_id))
        .where(work_credits.c.work_id.in_(ids))
        .order_by(work_credits.c.work_id, work_credits.c.position, work_credits.c.id)
    )
    grouped: dict[uuid.UUID, list[Row[Any]]] = {}
    for row in rows:
        grouped.setdefault(row.work_id, []).append(row)
    return grouped


async def title_matches(conn: AsyncConnection, term: str) -> list[uuid.UUID]:
    """Work ids whose indexed titles match ``term`` (FR-028, R5).

    Uses :func:`~aggregato.db.search.matching_ref_ids` rather than a subquery, because rendering a
    UUID to text differs between dialects and the subquery form silently matches nothing on one.
    Ids the index holds that are no longer parseable as UUIDs are skipped rather than raising.
    """
    found: list[uuid.UUID] = []
    for ref_id in await matching_ref_ids(conn, SearchKind.WORK_TITLE, term):
        try:
            found.append(uuid.UUID(ref_id))
        except ValueError:  # pragma: no cover - only reachable from a hand-edited index
            continue
    return found


async def review_matches(conn: AsyncConnection, term: str) -> list[int]:
    """``provider_item_id``s whose indexed review text matches ``term``.

    The review index keys documents as ``"<provider_item_id>:<position>"`` (ingest/writer.py),
    since an opinion's surrogate id is not known until after the upsert. The provider item is the
    granularity a review-text search filters on: it is the record the review and entry came from.
    """
    found: list[int] = []
    for ref_id in await matching_ref_ids(conn, SearchKind.REVIEW_TEXT, term):
        item_id, _, _ = ref_id.partition(":")
        if item_id.isdigit():
            found.append(int(item_id))
    return found


def opinion_facts(*, include_deleted: bool) -> Any:
    """The per-provider-item rating and review facts an entry is scored and filtered by.

    An entry has no score of its own: the score belongs to an opinion. This joins the two on
    ``provider_item_id`` — the same platform record the ingest writer created both from — which is
    the tightest available pairing, and the only one that does not attribute a series rating to an
    episode watch.

    Returns:
        A subquery with ``provider_item_id``, ``score`` and ``reviewed`` columns.
    """
    statement = select(
        opinions.c.provider_item_id.label("provider_item_id"),
        func.max(opinions.c.rating_normalized).label("score"),
        func.max(case((opinions.c.review_text.is_not(None), 1), else_=0)).label("reviewed"),
    ).group_by(opinions.c.provider_item_id)
    if not include_deleted:
        statement = statement.where(opinions.c.deleted_at.is_(None))
    return statement.subquery("opinion_facts")
