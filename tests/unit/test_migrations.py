"""The initial revision and the startup hook .

Everything here runs against a real SQLite file in ``tmp_path``: migrations are DDL, and DDL is
only proved by executing it.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine

from aggregato.db import migrate
from aggregato.db.migrate import backup_sqlite, upgrade_to_head
from aggregato.db.schema import metadata
from aggregato.db.search import sync_create_search_index

REVISION = Path(migrate.__file__).parent / "migrations/versions/0001_initial_schema.py"


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _config(url: str) -> Config:
    """The same configuration ``upgrade_to_head`` builds, for tests that stop at a revision."""
    config = Config()
    config.set_main_option("script_location", str(migrate._SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _names(path: Path, kind: str) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
        ).fetchall()
    return {name for (name,) in rows}


def _columns(path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_upgrade_creates_every_table_in_metadata_plus_the_search_index(tmp_path: Path) -> None:
    db = tmp_path / "aggregato.db"
    upgrade_to_head(_url(db))

    tables = _names(db, "table")
    # The set, not a hardcoded list: schema.py is the source of truth for what must exist.
    missing = set(metadata.tables) - tables
    assert not missing, f"revision does not create {sorted(missing)}"
    assert "search_index" in tables, "the dialect-specific search index was not created "
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
    # Columns too, not just table and index names: a revision that adds a column to an existing
    # table drifts invisibly otherwise — every table still exists, so the assertions above pass
    # while the column the code reads is missing from a migrated database.
    for table in _names(expected, "table"):
        assert _columns(migrated, table) == _columns(expected, table), (
            f"{table} columns differ between the revisions and schema.py"
        )


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


def test_head_rejects_the_retired_season_media_types(tmp_path: Path) -> None:
    """0005 narrows the vocabulary; under SQLite's batch rebuild the CHECK is easy to lose."""
    db = tmp_path / "aggregato.db"
    upgrade_to_head(_url(db))
    with closing(sqlite3.connect(db)) as conn:
        for media_type in ("tv_season", "anime_season"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at)"
                    " VALUES ('x', ?, 't', 't', '2026-01-01', '2026-01-01')",
                    (media_type,),
                )


def test_0005_keeps_the_log_it_retypes(tmp_path: Path) -> None:
    """A season's entries must survive the rebuild.

    SQLite cannot alter a CHECK, so 0005 rebuilds ``works`` in batch mode — and a rebuild DROPs the
    original table, whose implicit ``DELETE FROM`` fires every ON DELETE CASCADE aimed at it. With
    engine.py's ``foreign_keys=ON`` that empties ``entries`` and ``opinions``: the migration deletes
    the log it was only supposed to retype.
    """
    db = tmp_path / "aggregato.db"
    config = _config(_url(db))
    command.upgrade(config, "0004")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at)"
            " VALUES ('w', 'anime_season', 't', 't', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO provider_items (id, provider_id, native_id, title_as_given, raw_payload,"
            " schema_version, first_seen_at, last_seen_at)"
            " VALUES (1, 'anilist', 'n', 't', '{}', 1, '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO entries (work_id, provider_id, provider_item_id, kind, logged_at,"
            " logged_precision, ingested_at)"
            " VALUES ('w', 'anilist', 1, 'watch', '2026-01-01', 'exact', '2026-01-01')"
        )
        conn.commit()

    # Stops at 0005 rather than head: this pins 0005's own behaviour, and a later revision that
    # renames the vocabulary again should not have to come back and edit this assertion.
    command.upgrade(config, "0005")

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM entries").fetchone() == (1,)
        assert conn.execute("SELECT media_type FROM works").fetchall() == [("anime_series",)]


def _seed_at_0006(db: Path, media_type: str) -> Config:
    """A work of ``media_type``, an entry hanging off it, and a merge_log snapshot naming it."""
    config = _config(_url(db))
    command.upgrade(config, "0006")
    snapshot = {"works": [{"id": "w", "media_type": media_type, "title": "t"}], "entries": []}
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at)"
            " VALUES ('w', ?, 't', 't', '2026-01-01', '2026-01-01')",
            (media_type,),
        )
        conn.execute(
            "INSERT INTO provider_items (id, provider_id, native_id, title_as_given, raw_payload,"
            " schema_version, first_seen_at, last_seen_at)"
            " VALUES (1, 'letterboxd', 'n', 't', '{}', 1, '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO entries (work_id, provider_id, provider_item_id, kind, logged_at,"
            " logged_precision, ingested_at)"
            " VALUES ('w', 'letterboxd', 1, 'watch', '2026-01-01', 'exact', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO merge_log (id, subject, operation, winner_id, loser_ids, performed_at,"
            " snapshot) VALUES (1, 'work', 'merge', 'w', '[]', '2026-01-01', ?)",
            (json.dumps(snapshot),),
        )
        conn.commit()
    return config


@pytest.mark.parametrize(("old", "new"), [("tv_series", "tv"), ("anime_series", "anime")])
def test_0007_renames_the_media_type_without_losing_the_log(
    tmp_path: Path, old: str, new: str
) -> None:
    """The rename must not repeat 0005's mistake: the rebuild it needs must keep every child row."""
    db = tmp_path / "aggregato.db"
    config = _seed_at_0006(db, old)

    command.upgrade(config, "0007")

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT media_type FROM works").fetchall() == [(new,)]
        # The whole point of the guard around the batch rebuild.
        assert conn.execute("SELECT count(*) FROM entries").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM provider_items").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at)"
                " VALUES ('x', ?, 't', 't', '2026-01-01', '2026-01-01')",
                (old,),
            )


@pytest.mark.parametrize(("old", "new"), [("tv_series", "tv"), ("anime_series", "anime")])
def test_0007_renames_inside_merge_log_snapshots(tmp_path: Path, old: str, new: str) -> None:
    """An undo restores the snapshotted rows verbatim, so a stale name there breaks the undo."""
    db = tmp_path / "aggregato.db"
    config = _seed_at_0006(db, old)

    command.upgrade(config, "head")

    with closing(sqlite3.connect(db)) as conn:
        snapshot = json.loads(conn.execute("SELECT snapshot FROM merge_log").fetchone()[0])
    assert snapshot["works"][0]["media_type"] == new
    assert snapshot["works"][0]["title"] == "t", "the walk rewrote more than media_type"


@pytest.mark.parametrize(("old", "new"), [("tv_series", "tv"), ("anime_series", "anime")])
def test_0007_downgrade_restores_the_previous_names(tmp_path: Path, old: str, new: str) -> None:
    """A pure rename is reversible, which is exactly what 0005's merge could not be."""
    db = tmp_path / "aggregato.db"
    config = _seed_at_0006(db, old)
    command.upgrade(config, "0007")

    command.downgrade(config, "0006")

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT media_type FROM works").fetchall() == [(old,)]
        assert conn.execute("SELECT count(*) FROM entries").fetchone() == (1,)
        snapshot = json.loads(conn.execute("SELECT snapshot FROM merge_log").fetchone()[0])
        assert snapshot["works"][0]["media_type"] == old
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at)"
                " VALUES ('x', ?, 't', 't', '2026-01-01', '2026-01-01')",
                (new,),
            )


def test_0008_repairs_duplicate_creator_identifiers_before_constraining_them(
    tmp_path: Path,
) -> None:
    """A broken archive upgrades by merging the duplicate identity rather than failing DDL."""
    db = tmp_path / "aggregato.db"
    config = _config(_url(db))
    command.upgrade(config, "0007")
    winner, loser = "1" * 32, "2" * 32
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO creators (id, kind, name, sort_name, metadata, created_at, updated_at)"
            " VALUES (?, 'person', 'Alex', 'alex', '{}', '2026-01-01', '2026-01-01')",
            (winner,),
        )
        conn.execute(
            "INSERT INTO creators (id, kind, name, sort_name, metadata, created_at, updated_at)"
            " VALUES (?, 'person', 'Alex', 'alex', '{}', '2026-01-02', '2026-01-02')",
            (loser,),
        )
        for creator_id in (winner, loser):
            conn.execute(
                "INSERT INTO creator_aliases "
                "(creator_id, name, normalized, media_family, kind, source)"
                " VALUES (?, 'Alex', 'alex', 'screen', 'primary', 'anilist')",
                (creator_id,),
            )
            conn.execute(
                "INSERT INTO creator_external_ids "
                "(creator_id, namespace, value, source, confidence)"
                " VALUES (?, 'anilist', '7', 'anilist', 'asserted')",
                (creator_id,),
            )
        conn.commit()

    command.upgrade(config, "head")

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT id FROM creators").fetchall() == [(winner,)]
        assert conn.execute("SELECT creator_id FROM creator_external_ids").fetchall() == [(winner,)]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO creator_external_ids "
                "(creator_id, namespace, value, source, confidence)"
                " VALUES (?, 'anilist', '7', 'anilist', 'asserted')",
                (winner,),
            )
