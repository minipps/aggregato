"""The one place idempotency is implemented (FR-005).

A resync must write nothing new, and it is a unique constraint plus ``ON CONFLICT`` that guarantees
that — not a pre-``SELECT``, which races. Both supported dialects speak ``on_conflict_do_update``
(research.md R4), so every writer in the codebase goes through :func:`upsert_stmt` and no writer
ever hand-rolls a check-then-insert.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.schema import ColumnCollectionConstraint


def upsert_stmt(
    bind: AsyncEngine | AsyncConnection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    *,
    index_elements: Sequence[str] | None = None,
    constraint: str | None = None,
    update_columns: Sequence[str] | None = None,
) -> Insert:
    """Build an "insert these rows, or update these columns on conflict" statement.

    Inputs:
      ``bind`` — the engine or connection the statement will run on. The dialect is read from it
      rather than from a global flag, so the same call site works on either backend (FR-048).
      ``table`` — the target table.
      ``rows`` — one or more mappings of column name to value; a batch is a single statement.
      ``index_elements`` / ``constraint`` — the conflict target: column names, or the name of a
      unique index or constraint. Exactly one of the two is required. A named target works on both
      dialects: SQLite has no ``ON CONFLICT ON CONSTRAINT``, so the name is resolved to its columns
      from ``table``.
      ``update_columns`` — columns to overwrite from the incoming row on conflict. Omit or pass an
      empty sequence for ``ON CONFLICT DO NOTHING``, which the ingest writer needs for rows that
      must never be revised once stored.

    Failure modes: ``ValueError`` if ``rows`` is empty, if the conflict target is absent, doubly
    specified or names nothing on ``table``, or if the dialect is neither SQLite nor Postgres. On a
    ``do nothing`` call the returned statement reports ``rowcount`` 0 for skipped rows, which is the
    expected outcome of a resync, not an error. Constraint violations on columns
    *outside* the conflict target still raise at execute time — this suppresses one conflict, not
    every conflict.
    """
    if not rows:
        raise ValueError("upsert_stmt needs at least one row")
    if (index_elements is None) == (constraint is None):
        raise ValueError("pass exactly one of index_elements or constraint")

    dialect = bind.dialect.name
    if dialect == "postgresql":
        pg = postgresql.insert(table).values(list(rows))
        if not update_columns:
            return pg.on_conflict_do_nothing(index_elements=index_elements, constraint=constraint)
        return pg.on_conflict_do_update(
            index_elements=index_elements,
            constraint=constraint,
            # ``excluded`` is the row that would have been inserted, so the update takes the
            # incoming values without naming them twice.
            set_={name: pg.excluded[name] for name in update_columns},
        )
    if dialect == "sqlite":
        # SQLite has no ``ON CONFLICT ON CONSTRAINT``, so a named target is resolved to its column
        # list here. Callers therefore name the constraint once and both dialects work (FR-048).
        target = index_elements if constraint is None else _columns_of(table, constraint)
        lite = sqlite.insert(table).values(list(rows))
        if not update_columns:
            return lite.on_conflict_do_nothing(index_elements=target)
        return lite.on_conflict_do_update(
            index_elements=target,
            set_={name: lite.excluded[name] for name in update_columns},
        )
    raise ValueError(f"unsupported dialect {dialect!r}; expected sqlite or postgresql")


def _columns_of(table: Table, name: str) -> list[str]:
    """Return the column names of the unique constraint or unique index called ``name``."""
    for constraint in table.constraints:
        # Only column-collection constraints (unique, primary key) can be a conflict target; a
        # CHECK constraint has no column list to resolve.
        if constraint.name == name and isinstance(constraint, ColumnCollectionConstraint):
            return [c.name for c in constraint.columns]
    for index in table.indexes:
        if index.name == name:
            return [c.name for c in index.columns]
    raise ValueError(f"{table.name!r} has no constraint or index named {name!r}")
