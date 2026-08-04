"""Keyset cursor codec .

The load-bearing test here is :func:`test_paging_tied_sort_keys_visits_every_row_once`: the whole
reason the cursor carries ``id`` is bulk-imported history where thousands of entries share one
timestamp (research.md ), so tied keys are the case that must be proven, not the happy path.
That test drives a real ``sqlite://`` in-memory table through :func:`keyset_where` — in-process,
no socket, so the autouse socket blocker in ``tests/conftest.py`` stays satisfied.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    nulls_last,
    select,
)
from sqlalchemy.dialects import sqlite

from aggregato.api.pagination import (
    LIMIT_DEFAULT,
    LIMIT_MAX,
    LIMIT_MIN,
    Cursor,
    InvalidCursor,
    Page,
    SortOrder,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    keyset_where,
)

TIE = datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)

_metadata = MetaData()
entries = Table(
    "entries",
    _metadata,
    Column("id", String, primary_key=True),
    Column("logged_at", DateTime),
    Column("score", Integer),
)


@pytest.fixture
def conn() -> Iterator[Connection]:
    """An in-memory SQLite connection holding just the ``entries`` table used above."""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _metadata.create_all(connection)
            yield connection
    finally:
        engine.dispose()  # filterwarnings = ["error"] makes an unclosed connection a failure


# --- round-tripping ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [datetime(2024, 3, 1, 12, 0, 0, 123456, tzinfo=UTC), 0, 87, -1, None],
)
def test_round_trip_each_value_type(value: datetime | int | None) -> None:
    cursor = Cursor(sort="logged_at", value=value, id="e-1")
    assert decode_cursor(encode_cursor(cursor), "logged_at") == cursor


def test_datetime_round_trips_as_aware_utc() -> None:
    original = datetime(2024, 3, 1, 9, 30, 0, 500, tzinfo=timezone(timedelta(hours=-3)))
    decoded = decode_cursor(encode_cursor(Cursor("logged_at", original, "e-1")), "logged_at")

    assert isinstance(decoded.value, datetime)
    assert decoded.value.tzinfo is UTC  # aware and normalized, not a string reparsed later
    assert decoded.value == original  # same instant, microseconds intact


def test_int_does_not_come_back_as_a_string() -> None:
    decoded = decode_cursor(encode_cursor(Cursor("score", 7, "e-1")), "score")
    assert isinstance(decoded.value, int)
    assert decoded.value == 7


def test_null_score_round_trips_as_none() -> None:
    decoded = decode_cursor(encode_cursor(Cursor("score", None, "e-1")), "score")
    assert decoded.value is None


def test_bool_is_not_accepted_as_an_int_value() -> None:
    with pytest.raises(TypeError):
        encode_cursor(Cursor("score", True, "e-1"))  # type: ignore[arg-type]


# --- tied sort keys: the reason the id tiebreaker exists --------------------------------------


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_paging_tied_sort_keys_visits_every_row_once(conn: Connection, order: SortOrder) -> None:
    """25 rows sharing one timestamp, paged 4 at a time: every row exactly once, in order."""
    rows = [{"id": f"e-{i:03d}", "logged_at": TIE, "score": None} for i in range(25)]
    conn.execute(entries.insert(), rows)

    seen = _page_through(conn, order, limit=4)
    expected = [r["id"] for r in rows]
    assert seen == (expected if order == "asc" else list(reversed(expected)))
    assert len(set(seen)) == len(rows)  # no duplicate; the list equality already covers gaps


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_paging_mixed_ties_and_nulls_visits_every_row_once(
    conn: Connection, order: SortOrder
) -> None:
    """Nulls sort last in both directions; inside the null block only ``id`` advances."""
    conn.execute(
        entries.insert(),
        [
            {"id": "a", "logged_at": TIE, "score": None},
            {"id": "b", "logged_at": TIE, "score": None},
            {"id": "c", "logged_at": TIE, "score": None},
            {"id": "d", "logged_at": TIE + timedelta(hours=1), "score": None},
            {"id": "e", "logged_at": None, "score": None},
            {"id": "f", "logged_at": None, "score": None},
        ],
    )

    seen = _page_through(conn, order, limit=2)
    assert seen == (
        # ``id`` follows the requested direction inside the null block too, hence f before e.
        ["a", "b", "c", "d", "e", "f"] if order == "asc" else ["d", "c", "b", "a", "f", "e"]
    )


# --- garbage in, clean failure out ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not base64 at all!!",
        "!!!!",
        "eyJrIjoi",  # valid base64 of truncated JSON
        "WyJub3QiLCJhbiIsIm9iamVjdCJd",  # a JSON array, not an object
        "bnVsbA",  # JSON null
        "e30",  # an empty JSON object: no sort key, no id
    ],
)
def test_malformed_cursor_raises_invalid_cursor(raw: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(raw, "logged_at")


def test_truncated_valid_cursor_raises_invalid_cursor() -> None:
    good = encode_cursor(Cursor("logged_at", TIE, "e-1"))
    with pytest.raises(InvalidCursor):
        decode_cursor(good[: len(good) // 2], "logged_at")


def test_cursor_from_a_different_sort_is_rejected() -> None:
    good = encode_cursor(Cursor("score", 50, "e-1"))
    with pytest.raises(InvalidCursor):
        decode_cursor(good, "logged_at")


def test_cursor_without_id_is_rejected() -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(encode_cursor(Cursor("logged_at", TIE, "")), "logged_at")


@pytest.mark.parametrize(
    "payload",
    [
        {"k": "logged_at", "t": "dt", "v": "2024-03-01T12:00:00", "id": "e-1"},  # no offset
        {"k": "logged_at", "t": "dt", "v": "the first of March", "id": "e-1"},
        {"k": "logged_at", "t": "date", "v": "2024-03-01", "id": "e-1"},  # unknown type tag
        {"k": "logged_at", "t": "int", "v": "50", "id": "e-1"},  # tag disagrees with the value
    ],
)
def test_forged_payloads_raise_invalid_cursor(payload: dict[str, object]) -> None:
    """A hand-built cursor cannot smuggle in a naive timestamp or a type the codec never issues."""
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(InvalidCursor):
        decode_cursor(raw, "logged_at")


def test_invalid_cursor_is_catchable_as_value_error() -> None:
    # Routes map it to a 400; a bare `except ValueError` must not swallow it into a restart.
    assert issubclass(InvalidCursor, ValueError)


# --- limit clamp -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0, LIMIT_MIN), (1, 1), (50, 50), (200, 200), (201, LIMIT_MAX), (-5, LIMIT_MIN)],
)
def test_limit_clamp(requested: int, expected: int) -> None:
    assert clamp_limit(requested) == expected


def test_limit_default() -> None:
    assert clamp_limit(None) == LIMIT_DEFAULT == 50


# --- compiled SQL, no database ----------------------------------------------------------------


def _compiled(order: SortOrder, value: datetime | None = TIE) -> str:
    clause = keyset_where(
        entries.c.logged_at, entries.c.id, order, Cursor("logged_at", value, "e-1")
    )
    statement = select(entries.c.id).where(clause)
    return str(
        statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
    ).replace("\n", " ")


@pytest.mark.parametrize(("order", "op"), [("asc", ">"), ("desc", "<")])
def test_where_clause_is_a_row_comparison_with_the_id_tiebreaker(order: SortOrder, op: str) -> None:
    sql = _compiled(order)
    assert f"(entries.logged_at, entries.id) {op} ('2024-03-01 12:00:00.000000', 'e-1')" in sql
    assert "entries.logged_at IS NULL" in sql  # nulls sort last, so they are still to come


@pytest.mark.parametrize(("order", "op"), [("asc", ">"), ("desc", "<")])
def test_where_clause_for_a_null_cursor_advances_by_id_only(order: SortOrder, op: str) -> None:
    sql = _compiled(order, value=None)
    assert "entries.logged_at IS NULL" in sql
    assert f"entries.id {op} 'e-1'" in sql
    assert "logged_at >" not in sql
    assert "logged_at <" not in sql


# --- helper ------------------------------------------------------------------------------------


def _page_through(conn: Connection, order: SortOrder, limit: int) -> list[str]:
    """Walk every page of ``entries`` sorted by ``logged_at`` using only cursors.

    Mirrors what a route does: order by ``(sort_col nulls last, id)`` in the requested direction,
    take ``limit`` rows, mint a cursor from the last one.
    """
    sort_col = entries.c.logged_at
    direction = (lambda c: c.asc()) if order == "asc" else (lambda c: c.desc())
    seen: list[str] = []
    cursor: Cursor | None = None

    for _ in range(1000):  # bounded so a cursor that stops advancing fails instead of hanging
        statement = select(entries.c.id, sort_col).order_by(
            nulls_last(direction(sort_col)), direction(entries.c.id)
        )
        if cursor is not None:
            statement = statement.where(keyset_where(sort_col, entries.c.id, order, cursor))
        page = _to_page(conn, statement, limit)
        seen.extend(page.items)
        if page.next_cursor is None:
            return seen
        cursor = decode_cursor(page.next_cursor, "logged_at")
    raise AssertionError("paging did not terminate")


def _to_page(conn: Connection, statement: object, limit: int) -> Page[str]:
    rows = conn.execute(statement.limit(limit + 1)).all()  # type: ignore[attr-defined]
    window = rows[:limit]
    next_cursor = None
    if len(rows) > limit and window:
        last_id, last_value = window[-1]
        # SQLite hands datetimes back naive; the codec requires an aware UTC value, and the route
        # would be reading an aware column anyway.
        if isinstance(last_value, datetime) and last_value.tzinfo is None:
            last_value = last_value.replace(tzinfo=UTC)
        next_cursor = encode_cursor(Cursor("logged_at", last_value, last_id))
    return Page(items=[row_id for row_id, _ in window], next_cursor=next_cursor)
