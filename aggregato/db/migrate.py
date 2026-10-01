"""Apply pending migrations at startup, backing up an embedded database first.

Alembic owns the version table and the revision chain; this module is only the hook that runs it,
so there is no second migration mechanism to keep in step.

``upgrade_to_head`` is synchronous. Call it outside a running event loop, such as in a dedicated
thread, because Alembic's environment runs its own ``asyncio.run``. SQLite backups use the database
backup API so committed WAL contents are included.
"""

from __future__ import annotations

import fcntl
import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from aggregato.domain.clock import SYSTEM_CLOCK

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
    stamp = SYSTEM_CLOCK.now().strftime("%Y%m%dT%H%M%S%f")
    backup = source.with_name(f"{source.name}.{stamp}.bak")
    suffix = 1
    while backup.exists():
        backup = source.with_name(f"{source.name}.{stamp}.{suffix}.bak")
        suffix += 1
    # sqlite3's backup API rather than shutil.copy2, for one reason: WAL. A plain copy of the `.db`
    # alone silently drops every committed transaction still sitting in the `-wal` file, which is
    # the normal state of this database (engine.py sets journal_mode=WAL). Same few lines, correct
    # under WAL, and it does not care whether another connection is open.
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(backup)) as dst:
        src.backup(dst)
    return backup


def upgrade_to_head(url: str) -> None:
    """Migrate ``url`` to the latest revision, backing up SQLite only when work is pending.

    Idempotent: already at head means Alembic runs nothing. The URL comes from the caller
    (``aggregato.config``), never from alembic.ini.
    """
    parsed = make_url(url)
    lock_path: Path | None = None
    if parsed.get_backend_name() == "sqlite" and parsed.database not in {None, ":memory:"}:
        lock_path = Path(parsed.database).with_name(f"{Path(parsed.database).name}.migration.lock")

    def migrate() -> None:
        config = Config()
        config.set_main_option("script_location", str(_SCRIPT_LOCATION))
        # `%` doubled because ConfigParser interpolates main options, and a URL-encoded password
        # is allowed to contain one.
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        if parsed.get_backend_name() == "sqlite" and parsed.database not in {None, ":memory:"}:
            database = Path(parsed.database)
            if _sqlite_is_at_head(database, config):
                return
            backup_sqlite(url)
        command.upgrade(config, "head")

    if lock_path is None:
        migrate()
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            migrate()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _sqlite_is_at_head(database: Path, config: Config) -> bool:
    """Check migration state while the caller holds the SQLite migration lock."""
    if not database.is_file():
        return False
    with closing(sqlite3.connect(database)) as conn:
        has_version_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
        ).fetchone()
        if has_version_table is None:
            return False
        current = {row[0] for row in conn.execute("SELECT version_num FROM alembic_version")}
    return current == set(ScriptDirectory.from_config(config).get_heads())
