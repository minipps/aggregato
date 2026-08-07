"""Alembic's runtime environment (, data-model.md §6 "Migrations").

Online mode only. ``--sql`` offline generation is not supported: migrations are applied
automatically at startup against a live database, so a SQL script nobody runs is dead code.

The URL always comes from the caller — ``-x url=...`` on the CLI, or the main option that
:func:`aggregato.db.migrate.upgrade_to_head` sets from ``aggregato.config``. Never from
``alembic.ini``.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.engine import make_url

from aggregato.db.engine import create_engine
from aggregato.db.schema import metadata

target_metadata = metadata


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or context.config.get_main_option(
        "sqlalchemy.url"
    )
    if not url:
        raise RuntimeError(
            "no database URL: pass `-x url=...` or call aggregato.db.migrate.upgrade_to_head"
        )
    return url


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER a column or drop a constraint, so any future revision that changes
        # a table has to copy-rename it. Batch mode is that dance, and it must be on before the
        # first revision needs it — SQLite is a first-class dialect here , not a fallback.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_locked_postgres_migrations(connection: Connection) -> None:
    """Serialize API/worker migration startup on one Postgres database."""
    connection.exec_driver_sql("SELECT pg_advisory_lock(482901734)")
    try:
        _run_migrations(connection)
    finally:
        connection.exec_driver_sql("SELECT pg_advisory_unlock(482901734)")


async def _main() -> None:
    url = _url()
    # Alembic is called from a synchronous migration hook. Using aiosqlite here starts a worker
    # thread and its event-loop handoff can deadlock under the test socket guard; application reads
    # and writes remain async. A synchronous SQLite connection is also the natural shape for DDL.
    if make_url(url).get_backend_name() == "sqlite":
        engine = create_sync_engine(
            make_url(url).set(drivername="sqlite"),
            connect_args={"check_same_thread": False},
        )
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                connection.commit()
                _run_migrations(connection)
        finally:
            engine.dispose()
        return

    # PostgreSQL uses the project's async engine because asyncpg is the supported optional driver.
    engine = create_engine(url)
    try:
        async with engine.connect() as connection:
            # Session-level locking avoids a version-table race between API and worker cold starts.
            await connection.run_sync(_run_locked_postgres_migrations)
    finally:
        await engine.dispose()


asyncio.run(_main())
