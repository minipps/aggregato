"""Alembic's runtime environment (FR-049, data-model.md §6 "Migrations").

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
        # first revision needs it — SQLite is a first-class dialect here (FR-048), not a fallback.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    # The project engine rather than Alembic's own: it attaches the SQLite PRAGMAs (foreign_keys,
    # WAL), so the migration runs under the same connection settings as the application.
    engine = create_engine(_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


asyncio.run(_main())
