"""The schema must stay first-class on both dialects (FR-048, research.md R4).

These are cheap compile-time checks rather than round-trips against a server, and they catch the
class of mistake that is otherwise found only by a self-hoster running the non-default dialect:
a Postgres-only type, a native enum, a partial index one dialect silently drops.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from aggregato.db.schema import entries, metadata, provider_items

DIALECTS: list[tuple[str, Dialect]] = [
    ("sqlite", sqlite.dialect()),
    ("postgresql", postgresql.dialect()),
]


@pytest.mark.parametrize(("name", "dialect"), DIALECTS)
def test_every_table_compiles(name: str, dialect: Dialect) -> None:
    for table in metadata.sorted_tables:
        CreateTable(table).compile(dialect=dialect)


@pytest.mark.parametrize(("name", "dialect"), DIALECTS)
def test_every_index_compiles(name: str, dialect: Dialect) -> None:
    for table in metadata.sorted_tables:
        for index in table.indexes:
            CreateIndex(index).compile(dialect=dialect)


def test_no_native_database_enums() -> None:
    """Every vocabulary is text plus a CHECK, never a native enum.

    A native enum is the trap: each added ``media_type`` becomes a Postgres ``ALTER TYPE`` with no
    SQLite equivalent, whereas a text column plus CHECK migrates identically on both.
    """
    offenders = [
        f"{table.name}.{column.name}"
        for table in metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, SAEnum)
    ]
    assert offenders == [], f"native database enums found: {offenders}"


def test_no_naive_timestamp_columns() -> None:
    """Every timestamp is timezone-aware, because everything is stored UTC (data-model.md)."""
    naive = [
        f"{table.name}.{column.name}"
        for table in metadata.sorted_tables
        for column in table.columns
        if column.type.__class__.__name__ == "DateTime"
        and not getattr(column.type, "timezone", False)
    ]
    assert naive == [], f"naive timestamp columns: {naive}"


def test_entries_partial_unique_index_is_emitted_by_both_dialects() -> None:
    """The idempotency key for identified events, and the reason it must be partial.

    Entries whose platform gives the event no id carry ``native_id IS NULL``. A plain unique index
    would make two such nulls collide on Postgres semantics or not at all on others; the partial
    predicate is what keeps unidentified events out of the constraint entirely, leaving them to the
    writer's ``(provider_item_id, kind, logged_at, subject_ref)`` fallback.
    """
    index = next(i for i in entries.indexes if i.name == "uq_entries_provider_id_native_id")
    assert index.unique
    for _, dialect in DIALECTS:
        sql = str(CreateIndex(index).compile(dialect=dialect))
        assert "WHERE native_id IS NOT NULL" in sql, sql


def test_provider_items_idempotency_key_exists() -> None:
    """FR-005 rests on this one constraint: a resync writes nothing new because of it."""
    keys = {
        tuple(c.name for c in constraint.columns)
        for constraint in provider_items.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("provider_id", "native_id") in keys


def test_no_user_id_column_anywhere() -> None:
    """FR-006 — single-user by construction. Adding one is a v2 schema break, accepted knowingly."""
    offenders = [
        f"{table.name}.{column.name}"
        for table in metadata.sorted_tables
        for column in table.columns
        if column.name in {"user_id", "owner_id", "account_id"}
    ]
    assert offenders == [], f"multi-user columns crept in: {offenders}"


def test_sqlite_autoincrement_keys_use_the_integer_variant() -> None:
    """Only ``INTEGER PRIMARY KEY`` aliases rowid on SQLite.

    A ``BIGINT`` primary key compiles fine and then silently stops auto-incrementing, which shows up
    much later as an integrity error on the second insert.
    """
    dialect = sqlite.dialect()
    for table in metadata.sorted_tables:
        for column in table.primary_key.columns:
            if not column.autoincrement or column.autoincrement == "auto":
                continue
            rendered = column.type.dialect_impl(dialect).compile(dialect=dialect)
            assert rendered == "INTEGER", f"{table.name}.{column.name} renders as {rendered}"
