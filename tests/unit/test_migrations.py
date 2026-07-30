"""The initial revision and the startup hook (FR-049, T021).

Everything here runs against a real SQLite file in ``tmp_path``: migrations are DDL, and DDL is
only proved by executing it.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine as create_sync_engine

from aggregato.db import migrate
from aggregato.db.migrate import backup_sqlite, upgrade_to_head
from aggregato.db.schema import metadata
from aggregato.db.search import sync_create_search_index

REVISION = Path(migrate.__file__).parent / "migrations/versions/0001_initial_schema.py"


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _names(path: Path, kind: str) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
        ).fetchall()
    return {name for (name,) in rows}


def test_upgrade_creates_every_table_in_metadata_plus_the_search_index(tmp_path: Path) -> None:
    db = tmp_path / "aggregato.db"
    upgrade_to_head(_url(db))

    tables = _names(db, "table")
    # The set, not a hardcoded list: schema.py is the source of truth for what must exist.
    missing = set(metadata.tables) - tables
    assert not missing, f"revision does not create {sorted(missing)}"
    assert "search_index" in tables, "the dialect-specific search index was not created (FR-028)"
    assert "alembic_version" in tables


def test_revision_matches_schema_py(tmp_path: Path) -> None:
    """The hand-written revision must build what ``metadata`` would.

    This is the drift check the initial revision's docstring promises — and specifically it catches
    the five expression-based indexes (``logged_at DESC`` …) that ``--autogenerate`` cannot see.
    """
    migrated = tmp_path / "migrated.db"
    upgrade_to_head(_url(migrated))

    expected = tmp_path / "expected.db"
    engine = create_sync_engine(f"sqlite:///{expected}")
    with engine.begin() as conn:
        metadata.create_all(conn)
        sync_create_search_index(conn)
    engine.dispose()

    assert _names(migrated, "table") - {"alembic_version"} == _names(expected, "table")
    assert _names(migrated, "index") == _names(expected, "index")


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "aggregato.db"
    upgrade_to_head(_url(db))
    before = _names(db, "table")
    upgrade_to_head(_url(db))
    assert _names(db, "table") == before


def test_pragmas_still_behave_after_migrating(tmp_path: Path) -> None:
    db = tmp_path / "aggregato.db"
    upgrade_to_head(_url(db))

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            # A dangling FK must be refused, which proves the constraint survived the migration.
            conn.execute(
                "INSERT INTO provider_state (provider_id, effective_interval_seconds) "
                "VALUES ('nope', 3600)"
            )


def test_backup_is_taken_before_migrating_and_holds_the_old_content(tmp_path: Path) -> None:
    db = tmp_path / "aggregato.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE sentinel (v TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('before')")
        conn.commit()

    upgrade_to_head(_url(db))

    backups = list(tmp_path.glob("aggregato.db.*.bak"))
    assert len(backups) == 1
    assert _names(backups[0], "table") == {"sentinel"}
    with closing(sqlite3.connect(backups[0])) as conn:
        assert conn.execute("SELECT v FROM sentinel").fetchone() == ("before",)


def test_no_backup_when_the_database_does_not_exist_yet(tmp_path: Path) -> None:
    db = tmp_path / "aggregato.db"
    assert backup_sqlite(_url(db)) is None
    upgrade_to_head(_url(db))
    assert list(tmp_path.glob("*.bak")) == []


def test_backup_ignores_a_non_sqlite_url() -> None:
    # No connection is attempted — the autouse socket blocker would fail the test if one were.
    assert backup_sqlite("postgresql+asyncpg://user:pw@db.example/aggregato") is None


def test_downgrade_is_refused() -> None:
    """Forward-only is a deliberate policy (data-model.md §6): the way back is the backup."""
    spec = importlib.util.spec_from_file_location("initial_revision", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(NotImplementedError):
        module.downgrade()
