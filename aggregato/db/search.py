"""Free-text search over titles and review text (, research.md ).

Two real implementations behind one interface, which is the one place in the codebase where that is
justified: SQLite uses an FTS5 virtual table, Postgres a ``tsvector`` column with a GIN index.
``LIKE '%term%'`` is not a third option — it cannot deliver 's 1 s p95 first page over a
million entries, and performance guidance says budgets are measured rather than asserted.

The index is maintained by the ingest writer **inside the same transaction as the row it
describes**, not by database triggers. That keeps one implementation of *when* to index, and avoids
writing trigger DDL twice, once per dialect.

The schema differs between dialects — a virtual table on one, a real table with a generated column
on the other — so this index cannot live in ``schema.py``'s ``MetaData``.
:func:`create_search_index` emits the right DDL, and the Alembic revision calls it.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import ColumnElement, Connection, Text, column, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import provider_items, works


class SearchKind(StrEnum):
    """What a search row describes, so one index serves both without them colliding."""

    WORK_TITLE = "work_title"
    REVIEW_TEXT = "review_text"


_SQLITE_DDL = (
    # kind and ref_id are UNINDEXED: they are filters and join keys, never search terms. Indexing
    # them would put "work_title" itself into the vocabulary and match every row.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
        kind UNINDEXED,
        ref_id UNINDEXED,
        content,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
)

_POSTGRES_DDL = (
    """
    CREATE TABLE IF NOT EXISTS search_index (
        id bigserial PRIMARY KEY,
        kind text NOT NULL,
        ref_id text NOT NULL,
        content text NOT NULL,
        tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_search_index_tsv ON search_index USING gin (tsv)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_search_index_kind_ref ON search_index (kind, ref_id)",
)


async def create_search_index(conn: AsyncConnection) -> None:
    """Create the dialect's search index if it does not exist.

    Called from the Alembic revision rather than at startup, so the DDL runs once under migration
    control like every other schema change .

    Raises:
        ValueError: The dialect is neither SQLite nor Postgres.
    """
    for statement in _ddl_for(conn.dialect.name):
        await conn.execute(text(statement))


def _ddl_for(dialect: str) -> tuple[str, ...]:
    if dialect == "sqlite":
        return _SQLITE_DDL
    if dialect == "postgresql":
        return _POSTGRES_DDL
    raise ValueError(f"unsupported dialect {dialect!r}; expected sqlite or postgresql")


async def index_document(
    conn: AsyncConnection,
    kind: SearchKind,
    ref_id: str,
    content: str,
) -> None:
    """Insert or replace one searchable document.

    Call this in the same transaction as the row it describes, so a rolled-back write cannot leave a
    searchable ghost behind.

    Args:
        conn: The connection already inside the writer's transaction.
        kind: Which sort of row this describes.
        ref_id: The row's identifier, as text — works are UUIDs and opinions are integers, and one
            column holding both beats two nullable ones.
        content: The text to index. For a work this is every title form joined together, so a search
            for an original-language title finds the work its operator logged under a translation.
    """
    dialect = conn.dialect.name
    if dialect == "sqlite":
        # FTS5 has no ON CONFLICT, so replace is delete-then-insert. Both statements are in the
        # caller's transaction, so there is no window where the document is missing.
        await conn.execute(
            text("DELETE FROM search_index WHERE kind = :kind AND ref_id = :ref_id"),
            {"kind": str(kind), "ref_id": ref_id},
        )
        await conn.execute(
            text(
                "INSERT INTO search_index (kind, ref_id, content) VALUES (:kind, :ref_id, :content)"
            ),
            {"kind": str(kind), "ref_id": ref_id, "content": content},
        )
        return
    if dialect == "postgresql":
        await conn.execute(
            text(
                "INSERT INTO search_index (kind, ref_id, content) "
                "VALUES (:kind, :ref_id, :content) "
                "ON CONFLICT (kind, ref_id) DO UPDATE SET content = EXCLUDED.content"
            ),
            {"kind": str(kind), "ref_id": ref_id, "content": content},
        )
        return
    raise ValueError(f"unsupported dialect {dialect!r}; expected sqlite or postgresql")


async def unindex_document(conn: AsyncConnection, kind: SearchKind, ref_id: str) -> None:
    """Remove a document from the index, for a merge that retires the losing row's id."""
    await conn.execute(
        text("DELETE FROM search_index WHERE kind = :kind AND ref_id = :ref_id"),
        {"kind": str(kind), "ref_id": ref_id},
    )


async def rebuild_work_document(conn: AsyncConnection, work_id: object) -> None:
    """Rebuild one work's title projection from the row and every provider title."""
    rows = await conn.execute(
        select(works.c.title, works.c.original_title, provider_items.c.title_as_given)
        .select_from(works.outerjoin(provider_items, provider_items.c.work_id == works.c.id))
        .where(works.c.id == work_id)
    )
    found = False
    forms: list[str] = []
    for row in rows:
        found = True
        for value in (row.title, row.original_title, row.title_as_given):
            if isinstance(value, str) and value and value not in forms:
                forms.append(value)
    if not found:
        await unindex_document(conn, SearchKind.WORK_TITLE, str(work_id))
        return
    await index_document(conn, SearchKind.WORK_TITLE, str(work_id), " ".join(forms))


def search_condition(
    dialect: str,
    target: ColumnElement[str],
    kind: SearchKind,
    term: str,
) -> ColumnElement[bool]:
    """Build the ``WHERE`` fragment restricting ``target`` to rows matching ``term``.

    Expressed as an ``IN (SELECT ref_id …)`` subquery rather than a join, because that is the one
    shape both dialects express identically while their match operators do not.

    Args:
        dialect: ``"sqlite"`` or ``"postgresql"``.
        target: The id column to constrain, cast to text by the caller where needed.
        kind: Which documents to search.
        term: Raw operator input. **Not** a query language — see :func:`_sanitize_sqlite_term`;
            a user typing ``AND`` or a stray quote gets a search for those characters, not a syntax
            error and not an injection.

    Returns:
        A boolean SQL expression.

    Raises:
        ValueError: Unsupported dialect.
    """
    if dialect == "sqlite":
        subquery = text(
            "SELECT ref_id FROM search_index WHERE kind = :search_kind "
            "AND search_index MATCH :search_term"
        ).bindparams(search_kind=str(kind), search_term=_sanitize_sqlite_term(term))
    elif dialect == "postgresql":
        # websearch_to_tsquery is the parser designed for untrusted input: it never raises on
        # malformed queries, where plainto_tsquery and to_tsquery do.
        subquery = text(
            "SELECT ref_id FROM search_index WHERE kind = :search_kind "
            "AND tsv @@ websearch_to_tsquery('simple', :search_term)"
        ).bindparams(search_kind=str(kind), search_term=term)
    else:
        raise ValueError(f"unsupported dialect {dialect!r}; expected sqlite or postgresql")
    # .columns() must NAME the returned column: an untyped TextualSelect has no column to
    # compare against, and `.in_()` fails on it rather than degrading.
    return target.in_(subquery.columns(column("ref_id", Text)))


async def matching_ref_ids(
    conn: AsyncConnection, kind: SearchKind, term: str, *, limit: int | None = None
) -> list[str]:
    """The ``ref_id``s matching ``term``, as text.

    Use this instead of :func:`search_condition` when the id being filtered is a **UUID**. The two
    dialects render a UUID to text differently — SQLite stores ``CHAR(32)`` with no dashes, Postgres
    casts to the canonical dashed form — so a ``CAST(id AS TEXT) IN (SELECT ref_id ...)`` subquery
    silently matches nothing on one of them. Fetching the ids and parsing them in Python is
    dialect-neutral. Callers may provide an explicit limit when they intentionally want a bounded
    candidate set; the default is complete so a large search result cannot silently lose matches.

    Args:
        conn: Any connection.
        kind: Which documents to search.
        term: Raw operator input; sanitized the same way as in :func:`search_condition`.
        limit: Optional maximum ids to return. ``None`` returns every match.

    Returns:
        Matching ``ref_id`` values, as stored.

    Raises:
        ValueError: Unsupported dialect.
    """
    dialect = conn.dialect.name
    if dialect == "sqlite":
        if limit is None:
            statement = text(
                "SELECT ref_id FROM search_index WHERE kind = :kind AND search_index MATCH :term"
            ).bindparams(kind=str(kind), term=_sanitize_sqlite_term(term))
        else:
            statement = text(
                "SELECT ref_id FROM search_index WHERE kind = :kind "
                "AND search_index MATCH :term LIMIT :limit"
            ).bindparams(kind=str(kind), term=_sanitize_sqlite_term(term), limit=limit)
    elif dialect == "postgresql":
        if limit is None:
            statement = text(
                "SELECT ref_id FROM search_index WHERE kind = :kind "
                "AND tsv @@ websearch_to_tsquery('simple', :term)"
            ).bindparams(kind=str(kind), term=term)
        else:
            statement = text(
                "SELECT ref_id FROM search_index WHERE kind = :kind "
                "AND tsv @@ websearch_to_tsquery('simple', :term) LIMIT :limit"
            ).bindparams(kind=str(kind), term=term, limit=limit)
    else:
        raise ValueError(f"unsupported dialect {dialect!r}; expected sqlite or postgresql")
    result = await conn.execute(statement)
    return [row[0] for row in result]


def _sanitize_sqlite_term(term: str) -> str:
    """Turn operator input into a literal FTS5 phrase query.

    FTS5's MATCH grammar treats bare ``AND``, ``OR``, ``NOT``, ``NEAR``, ``*``, ``(`` and ``:`` as
    operators, and raises a syntax error on unbalanced quotes. Someone searching for *Alien vs.
    Predator* or a title containing an apostrophe should get results, not a 500 — so each whitespace
    -separated token becomes a quoted phrase, with embedded quotes doubled per FTS5's escaping rule.

    The cost is that this deliberately supports no query syntax at all. That is the right trade for
    one user searching their own log; a query language is not a feature anyone asked for.
    """
    tokens = [t for t in term.split() if t]
    if not tokens:
        # An empty MATCH is a syntax error in FTS5. A term that sanitizes to nothing should match
        # nothing, and this phrase cannot appear in real text.
        return '""'
    return " ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def sync_create_search_index(conn: Connection) -> None:
    """Synchronous form, for Alembic's migration context, which runs on a sync connection."""
    for statement in _ddl_for(conn.dialect.name):
        conn.execute(text(statement))
