"""The idempotency helper and the engine's SQLite session settings, against a real database.

These run on actual aiosqlite with the real schema from ``aggregato.db.schema``: an upsert that
compiles but does not match the live constraint would pass a mock-based test and lose data in
production (FR-005).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import create_engine, transaction
from aggregato.db.schema import metadata, provider_items
from aggregato.db.upsert import upsert_stmt

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

# (provider_id, native_id) is the real idempotency key on provider_items.
KEY = ["provider_id", "native_id"]
UNIQUE_CONSTRAINT = "uq_provider_items_provider_id_native_id"


def row(native_id: str, title: str, *, seen: datetime = NOW, version: int = 1) -> dict[str, Any]:
    return {
        "provider_id": "trakt",
        "native_id": native_id,
        "title_as_given": title,
        "raw_payload": {"title": title},
        "schema_version": version,
        "first_seen_at": seen,
        "last_seen_at": seen,
    }


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A file-backed SQLite engine with the real schema. File-backed because WAL needs a file."""
    eng = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'aggregato.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def stored(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with transaction(engine) as conn:
        result = await conn.execute(
            select(provider_items).order_by(provider_items.c.native_id),
        )
        return [dict(m) for m in result.mappings()]


async def test_new_row_inserts(engine: AsyncEngine) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            upsert_stmt(
                conn,
                provider_items,
                [row("tt1", "Arrival")],
                index_elements=KEY,
                update_columns=["title_as_given", "last_seen_at"],
            )
        )

    rows = await stored(engine)
    assert [r["title_as_given"] for r in rows] == ["Arrival"]


async def test_conflict_updates_named_columns_only(engine: AsyncEngine) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            upsert_stmt(conn, provider_items, [row("tt1", "Arrival")], index_elements=KEY)
        )
        await conn.execute(
            upsert_stmt(
                conn,
                provider_items,
                [row("tt1", "Arrival (2016)", seen=LATER, version=2)],
                index_elements=KEY,
                update_columns=["title_as_given", "last_seen_at", "schema_version"],
            )
        )

    (stored_row,) = await stored(engine)
    assert stored_row["title_as_given"] == "Arrival (2016)"
    assert stored_row["last_seen_at"].replace(tzinfo=UTC) == LATER
    assert stored_row["schema_version"] == 2
    # Untouched: not in update_columns. first_seen_at must survive a resync.
    assert stored_row["first_seen_at"].replace(tzinfo=UTC) == NOW
    assert stored_row["raw_payload"] == {"title": "Arrival"}


async def test_do_nothing_leaves_existing_row(engine: AsyncEngine) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            upsert_stmt(conn, provider_items, [row("tt1", "Arrival")], index_elements=KEY)
        )
        await conn.execute(
            upsert_stmt(
                conn,
                provider_items,
                [row("tt1", "Overwritten?", seen=LATER, version=9)],
                index_elements=KEY,
            )
        )

    (stored_row,) = await stored(engine)
    assert stored_row["title_as_given"] == "Arrival"
    assert stored_row["schema_version"] == 1
    assert stored_row["last_seen_at"].replace(tzinfo=UTC) == NOW


async def test_mixed_batch_inserts_and_updates_in_one_statement(engine: AsyncEngine) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            upsert_stmt(conn, provider_items, [row("tt1", "Arrival")], index_elements=KEY)
        )
        await conn.execute(
            upsert_stmt(
                conn,
                provider_items,
                [
                    row("tt1", "Arrival (2016)", seen=LATER),
                    row("tt2", "Dune", seen=LATER),
                ],
                index_elements=KEY,
                update_columns=["title_as_given", "last_seen_at"],
            )
        )

    rows = await stored(engine)
    assert [(r["native_id"], r["title_as_given"]) for r in rows] == [
        ("tt1", "Arrival (2016)"),
        ("tt2", "Dune"),
    ]
    # The pre-existing row was updated, not duplicated, and its first_seen_at is intact.
    assert rows[0]["first_seen_at"].replace(tzinfo=UTC) == NOW


async def test_constraint_name_as_conflict_target_on_sqlite(engine: AsyncEngine) -> None:
    """A named target resolves to its columns, since SQLite has no ON CONFLICT ON CONSTRAINT."""
    async with transaction(engine) as conn:
        await conn.execute(
            upsert_stmt(conn, provider_items, [row("tt1", "Arrival")], index_elements=KEY)
        )
        await conn.execute(
            upsert_stmt(
                conn,
                provider_items,
                [row("tt1", "Arrival (2016)")],
                constraint=UNIQUE_CONSTRAINT,
                update_columns=["title_as_given"],
            )
        )

    (stored_row,) = await stored(engine)
    assert stored_row["title_as_given"] == "Arrival (2016)"


def test_compiles_for_postgresql() -> None:
    """Same call site, other dialect: FR-048 is only true if the statement compiles there too.

    Compile-only — building an asyncpg engine opens no connection, and the socket blocker in
    conftest would fail the test if it did.
    """
    pg_engine = create_engine("postgresql+asyncpg://aggregato@db/aggregato")
    stmt = upsert_stmt(
        pg_engine,
        provider_items,
        [row("tt1", "Arrival"), row("tt2", "Dune")],
        constraint=UNIQUE_CONSTRAINT,
        update_columns=["title_as_given", "last_seen_at"],
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert f"ON CONFLICT ON CONSTRAINT {UNIQUE_CONSTRAINT} DO UPDATE" in sql
    assert "title_as_given = excluded.title_as_given" in sql

    nothing = upsert_stmt(pg_engine, provider_items, [row("tt1", "Arrival")], index_elements=KEY)
    assert "ON CONFLICT (provider_id, native_id) DO NOTHING" in str(
        nothing.compile(dialect=postgresql.dialect())
    )


def test_unsupported_dialect_and_bad_arguments() -> None:
    with pytest.raises(ValueError, match="at least one row"):
        upsert_stmt(create_engine("sqlite+aiosqlite://"), provider_items, [], index_elements=KEY)
    with pytest.raises(ValueError, match="exactly one"):
        upsert_stmt(create_engine("sqlite+aiosqlite://"), provider_items, [row("tt1", "A")])
    with pytest.raises(ValueError, match="no constraint or index named"):
        upsert_stmt(
            create_engine("sqlite+aiosqlite://"),
            provider_items,
            [row("tt1", "A")],
            constraint="nope",
        )


async def test_engine_sets_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Foreign keys ON (SQLite defaults it off) and WAL on a file-backed database."""
    async with transaction(engine) as conn:
        foreign_keys = await conn.exec_driver_sql("PRAGMA foreign_keys")
        journal_mode = await conn.exec_driver_sql("PRAGMA journal_mode")
        busy_timeout = await conn.exec_driver_sql("PRAGMA busy_timeout")
        assert foreign_keys.scalar() == 1
        assert journal_mode.scalar() == "wal"
        assert busy_timeout.scalar() > 0
