"""Keyset pagination: the opaque cursor codec and its WHERE clause.

The cursor encodes the last row's sort value **plus its ``id``**, and the ``id`` is the entire
reason this module exists. A cursor holding only ``logged_at`` cannot page through a bulk-imported
history where thousands of entries share one timestamp: every row with that timestamp either
repeats on the next page or is skipped, depending on which way the comparison is written. Adding
``id`` makes the sort order *total*, so ``(sort_col, id)`` is unique and every row appears exactly
once (research.md ).

There is no offset parameter here or anywhere else — . ``OFFSET`` degrades linearly and the
spec names it a footgun.

The cursor is opaque, not secret. It is base64-encoded JSON, unsigned: signing it would introduce a
key to configure and rotate for no benefit, since the only thing it protects is a comparison value
the client already saw. What *is* required is failing cleanly — anything that is not a cursor this
module wrote raises :class:`InvalidCursor`, which the API renders as a 400. Never a 500, and never
a silent "start from the beginning", which would quietly re-serve page one forever.

Null ordering convention: a ``NULL`` sort value (an entry with no score) sorts **last in both
directions**. SQLite and Postgres disagree on the default, so callers must order by
``(sort_col IS NULL), sort_col <dir>, id <dir>`` to match the clause :func:`keyset_where` builds.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import ColumnElement, and_, literal, or_, tuple_

SortKey = Literal["logged_at", "ingested_at", "score"]
"""The ``sort`` values the ``/entries`` contract permits."""

CursorKey = SortKey | Literal["created_at", "updated_at"]
"""Every column a cursor can be issued for.

Wider than :data:`SortKey` because ``/works`` and ``/opinions`` have no ``sort`` parameter but
still page by keyset: they order by ``created_at`` and ``updated_at`` respectively. The key is
carried in the cursor so one endpoint's cursor cannot be replayed against another's column.
"""

SortOrder = Literal["asc", "desc"]
"""The ``order`` values the ``/entries`` contract permits."""

CursorValue = datetime | int | None
"""What a sort column can hold: a timestamp, a normalized score, or nothing."""

LIMIT_MIN: Final = 1
LIMIT_MAX: Final = 200
LIMIT_DEFAULT: Final = 50

_DATETIME_TAG: Final = "dt"
_INT_TAG: Final = "int"
_NULL_TAG: Final = "null"


class InvalidCursor(ValueError):
    """The ``cursor`` parameter is not a cursor this module produced.

    Raised for non-base64 input, truncated or corrupt payloads, a payload that is not the expected
    JSON object, an unknown value type, or a cursor taken from a page sorted by a different key.
    Callers turn this into a 400; treating it as "no cursor" would silently restart paging.
    """


@dataclass(frozen=True, slots=True)
class Cursor:
    """The position of the last row of a page, in the order that page was sorted.

    Attributes:
        sort: Which column the value belongs to. Carried so a cursor cannot be replayed against a
            different ``sort``, which would compare a score against a timestamp.
        value: The row's sort value. Datetimes are always aware UTC.
        id: The row's primary key — the tiebreaker that makes the order total.
    """

    sort: CursorKey
    value: CursorValue
    id: str


@dataclass(frozen=True, slots=True)
class Page[T]:
    """The ``Page`` response shape from the contract: ``items`` plus a nullable ``next_cursor``.

    Attributes:
        items: The rows of this page.
        next_cursor: Cursor for the following page, or ``None`` when this page is the last one.
    """

    items: list[T]
    next_cursor: str | None


def clamp_limit(limit: int | None) -> int:
    """Clamp a client-supplied ``limit`` into the range the contract allows.

    The cap is enforced server-side rather than merely documented, so an unbounded page cannot be
    requested (openapi.yaml: ``minimum: 1, maximum: 200, default: 50``).

    Args:
        limit: The requested page size, or ``None`` when the client did not ask.

    Returns:
        ``LIMIT_DEFAULT`` when ``limit`` is ``None``, otherwise ``limit`` clamped to
        ``[LIMIT_MIN, LIMIT_MAX]``. Out-of-range values are clamped rather than rejected — the
        contract states a maximum, not a validation rule, and a 400 on ``limit=500`` helps nobody.
    """
    if limit is None:
        return LIMIT_DEFAULT
    return max(LIMIT_MIN, min(LIMIT_MAX, limit))


def encode_cursor(cursor: Cursor) -> str:
    """Encode a cursor as the opaque string handed to the client as ``next_cursor``.

    Values carry a type tag so they round-trip exactly: a datetime comes back as an aware UTC
    datetime, not a string that a later parse might read differently, and an integer score does not
    come back as ``"7"``.

    Args:
        cursor: The position to encode.

    Returns:
        Unpadded URL-safe base64 of a compact JSON object.

    Raises:
        TypeError: If ``value`` is not a datetime, an int, or ``None``.
    """
    tag, raw = _encode_value(cursor.value)
    payload = json.dumps(
        {"k": cursor.sort, "t": tag, "v": raw, "id": cursor.id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(raw: str, sort: CursorKey) -> Cursor:
    """Decode a client-supplied cursor, or fail loudly.

    Args:
        raw: The ``cursor`` query parameter.
        sort: The ``sort`` the current request is using. A cursor minted under a different sort key
            is rejected, because comparing its value against this column is meaningless.

    Returns:
        The decoded :class:`Cursor`, with any datetime as an aware UTC datetime.

    Raises:
        InvalidCursor: For anything that is not a well-formed cursor for ``sort``.
    """
    try:
        # Re-pad: encode_cursor strips '=' so the cursor stays clean in a URL. validate=True makes
        # stray characters an error instead of being silently discarded, which is what turns a
        # truncated or hand-edited cursor into a clean 400.
        padded = raw + "=" * (-len(raw) % 4)
        payload = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        data = json.loads(payload)
    except (binascii.Error, UnicodeEncodeError, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor("cursor is not valid base64-encoded JSON") from exc

    if not isinstance(data, dict):
        raise InvalidCursor("cursor payload is not an object")
    if data.get("k") != sort:
        raise InvalidCursor(f"cursor was issued for a different sort than {sort!r}")
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise InvalidCursor("cursor is missing its id tiebreaker")

    return Cursor(sort=sort, value=_decode_value(data.get("t"), data.get("v")), id=data["id"])


def keyset_where(
    sort_col: ColumnElement[Any],
    id_col: ColumnElement[Any],
    order: SortOrder,
    cursor: Cursor,
) -> ColumnElement[bool]:
    """Build the "strictly after this row, in this order" predicate.

    For a non-null cursor value this is the row comparison ``(sort_col, id) < (:value, :id)`` for
    ``desc`` (``>`` for ``asc``), which is both compact and index-friendly — plus the rows whose
    sort value is ``NULL``, since nulls sort last in both directions (see the module docstring).
    Once the cursor itself sits in the null block, only ``id`` can advance.

    The table and columns are arguments rather than imports so this stays a pure unit under test
    with no engine and no schema.

    Args:
        sort_col: The column named by ``sort``.
        id_col: The primary key column used as tiebreaker.
        order: ``asc`` or ``desc`` — must be the order the cursor's page used.
        cursor: The decoded position of the last row of the previous page.

    Returns:
        A boolean SQLAlchemy expression to AND into the query's WHERE clause.

    Raises:
        InvalidCursor: The cursor's ``id`` cannot be read as ``id_col``'s own type — a hand-edited
            cursor claiming ``"abc"`` against a ``bigint`` key.
    """
    key_id = _typed_id(id_col, cursor.id)
    after_null = id_col > key_id if order == "asc" else id_col < key_id

    if cursor.value is None:
        # The previous page ended inside the null block, which is last; nothing non-null remains.
        return and_(sort_col.is_(None), after_null)

    row = tuple_(sort_col, id_col)
    key = tuple_(literal(cursor.value), key_id)
    strictly_after = row > key if order == "asc" else row < key
    # NULLs compare as NULL inside the row comparison above, so they must be added explicitly —
    # this is the clause that keeps unscored entries from vanishing from page two onward.
    return or_(strictly_after, sort_col.is_(None))


def _typed_id(id_col: ColumnElement[Any], raw: str) -> ColumnElement[Any]:
    """Bind the cursor's ``id`` as the id column's own type.

    The cursor carries the id as text, because one codec serves a ``bigint`` key (entries, opinions)
    and a UUID key (works). Comparing a text bind parameter against either of those is a per-dialect
    coin flip: Postgres rejects ``bigint < text`` outright, and SQLite silently sorts every integer
    before every string, which would hand back page one forever. So the value is converted to the
    column's Python type and bound with that type.
    """
    try:
        python_type = id_col.type.python_type
    except NotImplementedError:  # pragma: no cover - a column with no Python type
        return literal(raw)
    if python_type is str:
        return literal(raw)
    try:
        return literal(python_type(raw), id_col.type)
    except (TypeError, ValueError) as exc:
        raise InvalidCursor(f"cursor id {raw!r} is not a valid {python_type.__name__}") from exc


def _encode_value(value: CursorValue) -> tuple[str, Any]:
    """Tag a sort value for JSON, preserving its exact type on the way back."""
    match value:
        case None:
            return _NULL_TAG, None
        case datetime():
            # Stored as an offset-bearing ISO string in UTC: microseconds survive, and the offset
            # means a naive value can never sneak back out of the decoder.
            return _DATETIME_TAG, value.astimezone(UTC).isoformat()
        case bool():
            # bool is an int subclass; no sort column is boolean, so reject rather than coerce.
            raise TypeError("cursor values must be a datetime, an int, or None")
        case int():
            return _INT_TAG, value
        case _:
            raise TypeError("cursor values must be a datetime, an int, or None")


def _decode_value(tag: object, raw: object) -> CursorValue:
    """Reverse :func:`_encode_value`, raising :class:`InvalidCursor` on anything unexpected."""
    if tag == _NULL_TAG and raw is None:
        return None
    if tag == _INT_TAG and isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if tag == _DATETIME_TAG and isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise InvalidCursor("cursor timestamp is not a valid ISO 8601 value") from exc
        if parsed.tzinfo is None:
            raise InvalidCursor("cursor timestamp has no offset")
        return parsed.astimezone(UTC)
    raise InvalidCursor("cursor value type is not one this API issues")
