"""Search runs against both dialects (T022).

The SQLite path is exercised for real, because FTS5's MATCH grammar is where the interesting
failures live: a title containing ``AND`` or an apostrophe is a syntax error waiting to happen, and
"operator gets a 500 for searching Alien vs. Predator" is exactly the class of bug this file exists
to prevent. The Postgres path is compile-checked, since a server is not available here and FR-036
forbids one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import Table, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.engine import create_engine, transaction
from aggregato.db.search import (
    SearchKind,
    _sanitize_sqlite_term,
    create_search_index,
    index_document,
    search_condition,
    unindex_document,
)


def _fts5_available() -> bool:
    """Ask this SQLite build directly. ``sqlite3.compile_options`` does not exist."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()
    return True


FTS5_AVAILABLE = _fts5_available()

requires_fts5 = pytest.mark.skipif(
    not FTS5_AVAILABLE, reason="this SQLite build has no FTS5; the Postgres path is unaffected"
)


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'search.db'}")
    try:
        async with transaction(engine) as connection:
            await create_search_index(connection)
            yield connection
    finally:
        await engine.dispose()


# --- Sanitizing operator input ----------------------------------------------------------------
# Pure-function tests, so they run whether or not this build has FTS5.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("dune", '"dune"'),
        ("blade runner", '"blade" "runner"'),
        # Bare boolean words are FTS5 operators. Quoted, they are just words to look for.
        ("alien AND predator", '"alien" "AND" "predator"'),
        ("this OR that", '"this" "OR" "that"'),
        ("NEAR far", '"NEAR" "far"'),
        # An unbalanced quote is a syntax error in raw FTS5.
        ('the "quote', '"the" """quote"'),
        # A prefix star would silently change the query's meaning.
        ("dun*", '"dun*"'),
        ("  spaced   out  ", '"spaced" "out"'),
    ],
)
def test_operator_input_becomes_a_literal_phrase(raw: str, expected: str) -> None:
    assert _sanitize_sqlite_term(raw) == expected


def test_an_empty_term_matches_nothing_rather_than_erroring() -> None:
    # An empty MATCH is an FTS5 syntax error, and "" cannot appear in real text.
    assert _sanitize_sqlite_term("   ") == '""'


# --- The SQLite implementation ----------------------------------------------------------------


@requires_fts5
async def test_a_document_is_findable_after_indexing(conn: AsyncConnection) -> None:
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Dune Part Two")
    assert await _matching(conn, SearchKind.WORK_TITLE, "dune") == ["work-1"]


@requires_fts5
async def test_search_is_case_insensitive(conn: AsyncConnection) -> None:
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Dune Part Two")
    assert await _matching(conn, SearchKind.WORK_TITLE, "DUNE") == ["work-1"]


@requires_fts5
async def test_search_ignores_diacritics(conn: AsyncConnection) -> None:
    # An operator who logged "Amélie" should find it typing "amelie" — the tokenizer is configured
    # with remove_diacritics for exactly this.
    await index_document(
        conn, SearchKind.WORK_TITLE, "work-1", "Le Fabuleux Destin d'Amélie Poulain"
    )
    assert await _matching(conn, SearchKind.WORK_TITLE, "amelie") == ["work-1"]


@requires_fts5
async def test_kinds_do_not_collide(conn: AsyncConnection) -> None:
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Solaris")
    await index_document(
        conn, SearchKind.REVIEW_TEXT, "opinion-9", "Solaris was slow and wonderful"
    )

    assert await _matching(conn, SearchKind.WORK_TITLE, "solaris") == ["work-1"]
    assert await _matching(conn, SearchKind.REVIEW_TEXT, "solaris") == ["opinion-9"]


@requires_fts5
async def test_the_kind_column_is_not_itself_searchable(conn: AsyncConnection) -> None:
    """kind and ref_id are UNINDEXED, so "work_title" is not in the vocabulary.

    Without that, searching for the kind name would match every row in the index.
    """
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Solaris")
    assert await _matching(conn, SearchKind.WORK_TITLE, "work_title") == []


@requires_fts5
async def test_reindexing_replaces_rather_than_duplicates(conn: AsyncConnection) -> None:
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Working Title")
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Actual Title")

    assert await _matching(conn, SearchKind.WORK_TITLE, "actual") == ["work-1"]
    # The old text must be gone, or a corrected title stays findable under its mistake forever.
    assert await _matching(conn, SearchKind.WORK_TITLE, "working") == []


@requires_fts5
async def test_unindexing_removes_the_document(conn: AsyncConnection) -> None:
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Stalker")
    await unindex_document(conn, SearchKind.WORK_TITLE, "work-1")
    assert await _matching(conn, SearchKind.WORK_TITLE, "stalker") == []


@requires_fts5
@pytest.mark.parametrize(
    "hostile",
    [
        "alien AND predator",
        'unbalanced " quote',
        "star*",
        "NEAR(a b)",
        "colon:term",
        "(parenthesis",
        "-minus",
        "^caret",
        "'; DROP TABLE search_index; --",
    ],
)
async def test_hostile_input_returns_a_result_set_rather_than_an_error(
    conn: AsyncConnection, hostile: str
) -> None:
    """Every one of these is an FTS5 syntax error or an injection attempt if passed through raw."""
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Alien vs. Predator")
    result = await _matching(conn, SearchKind.WORK_TITLE, hostile)
    assert isinstance(result, list)
    # And the table is still there, which the next assertion would fail on if it were not.
    await index_document(conn, SearchKind.WORK_TITLE, "work-2", "Still Working")


@requires_fts5
async def test_a_title_containing_a_boolean_word_is_findable(conn: AsyncConnection) -> None:
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Alien vs. Predator")
    assert await _matching(conn, SearchKind.WORK_TITLE, "alien predator") == ["work-1"]


@requires_fts5
async def test_multiple_terms_narrow_rather_than_widen(conn: AsyncConnection) -> None:
    await index_document(conn, SearchKind.WORK_TITLE, "work-1", "Blade Runner 2049")
    await index_document(conn, SearchKind.WORK_TITLE, "work-2", "Blade")

    assert await _matching(conn, SearchKind.WORK_TITLE, "blade runner") == ["work-1"]


# --- The Postgres implementation --------------------------------------------------------------


def test_postgres_condition_compiles_and_uses_websearch_to_tsquery() -> None:
    """websearch_to_tsquery is the one parser that never raises on malformed operator input."""
    from aggregato.db.schema import works

    condition = search_condition("postgresql", _as_text(works), SearchKind.WORK_TITLE, "dune")
    sql = str(condition.compile(dialect=postgresql.dialect()))

    assert "websearch_to_tsquery" in sql
    assert "tsv @@" in sql
    # to_tsquery and plainto_tsquery both raise on input like "alien AND", so neither may appear.
    assert "plainto_tsquery" not in sql


def test_an_unsupported_dialect_is_rejected_rather_than_silently_degraded() -> None:
    from aggregato.db.schema import works

    with pytest.raises(ValueError, match="unsupported dialect"):
        search_condition("mysql", _as_text(works), SearchKind.WORK_TITLE, "dune")


# --- helpers ----------------------------------------------------------------------------------


def _as_text(table: Table) -> object:
    """The id column as text, which is how ref_id is stored for both UUID and integer keys."""
    from sqlalchemy import cast
    from sqlalchemy.types import Text

    return cast(table.c.id, Text)


async def _matching(conn: AsyncConnection, kind: SearchKind, term: str) -> list[str]:
    """Run the real subquery and return the ref_ids it selects, in insertion order."""
    from sqlalchemy import column, literal_column, table

    index = table("search_index", column("kind"), column("ref_id"))
    condition = search_condition("sqlite", literal_column("ref_id"), kind, term)
    result = await conn.execute(
        select(index.c.ref_id).where(index.c.kind == str(kind)).where(condition)
    )
    return [row[0] for row in result]
