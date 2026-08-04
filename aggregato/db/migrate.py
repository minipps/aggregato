"""Apply migrations at startup, backing up an embedded database first .

Alembic owns the version table and the revision chain; this module is only the hook that runs it,
so there is no second migration mechanism to keep in step (data-model.md §6).

``upgrade_to_head`` is **synchronous** and must be called before the event loop starts — Alembic's
environment runs its own ``asyncio.run``. That is also what makes the plain-file backup safe: at
that point nothing else has opened the database.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

__all__ = ["backup_sqlite", "upgrade_to_head"]

#: Shipped inside the package, so a wheel install finds it without alembic.ini (which exists for
#: the CLI only).
_SCRIPT_LOCATION = Path(__file__).resolve().parent / "migrations"


def backup_sqlite(url: str) -> Path | None:
    """Copy the SQLite database beside itself before anything migrates it.

    This is 's safety net: forward-only migrations have no downgrade (see the initial
    revision), so the file copy *is* the way back from a migration that goes wrong.

    Args:
        url: The database URL. Anything that is not an on-disk SQLite database is a no-op.

    Returns:
        The backup's path, or ``None`` for a non-SQLite URL, an in-memory database, or a file that
        does not exist yet (a first run has nothing to lose).
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    source = Path(database)
    if not source.exists():
        return None

    # Timestamped, never a fixed name: a second startup after a failed migration must not overwrite
    # the one good copy with the half-migrated file.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup = source.with_name(f"{source.name}.{stamp}.bak")
    # sqlite3's backup API rather than shutil.copy2, for one reason: WAL. A plain copy of the `.db`
    # alone silently drops every committed transaction still sitting in the `-wal` file, which is
    # the normal state of this database (engine.py sets journal_mode=WAL). Same few lines, correct
    # under WAL, and it does not care whether another connection is open.
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(backup)) as dst:
        src.backup(dst)
    return backup


def upgrade_to_head(url: str) -> None:
    """Migrate ``url`` to the latest revision, taking a backup first .

    Idempotent: already at head means Alembic runs nothing. The URL comes from the caller
    (``aggregato.config``), never from alembic.ini.
    """
    backup_sqlite(url)
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    # `%` doubled because ConfigParser interpolates main options, and a URL-encoded password is
    # allowed to contain one.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
