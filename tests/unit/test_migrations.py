"""The initial revision and the startup hook .

Everything here runs against a real SQLite file in ``tmp_path``: migrations are DDL, and DDL is
only proved by executing it.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
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


def test_upgrade_creates_a_missing_database_parent(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "data" / "aggregato.db"

    upgrade_to_head(_url(db))

    assert db.is_file()


def test_backup_name_collision_does_not_overwrite_the_previous_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "aggregato.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE sentinel (v TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('before')")
        conn.commit()

    fixed_now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

    class FixedClock:
        def now(self) -> datetime:
            return fixed_now

    monkeypatch.setattr(migrate, "SYSTEM_CLOCK", FixedClock())
    first = backup_sqlite(_url(db))
    second = backup_sqlite(_url(db))

    assert first is not None and second is not None
    assert first != second
    assert first.is_file() and second.is_file()
    assert set(tmp_path.glob("aggregato.db.*.bak")) == {first, second}


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
    assert list(tmp_path.glob("*.bak")) == []


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


def test_head_rejects_the_retired_podcast_media_types(tmp_path: Path) -> None:
    db = tmp_path / "aggregato.db"
    upgrade_to_head(_url(db))
    with closing(sqlite3.connect(db)) as conn:
        for media_type in ("podcast", "podcast_episode"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at)"
                    " VALUES ('x', ?, 't', 't', '2026-01-01', '2026-01-01')",
                    (media_type,),
                )


@pytest.mark.parametrize("retired", ["podcast", "podcast_episode"])
def test_0015_retypes_legacy_podcast_rows_without_losing_the_log(
    tmp_path: Path, retired: str
) -> None:
    db = tmp_path / "aggregato.db"
    config = _config(_url(db))
    command.upgrade(config, "0014")
    snapshot = {"works": [{"id": "w", "media_type": retired, "title": "t"}], "entries": []}
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at)"
            " VALUES ('w', ?, 't', 't', '2026-01-01', '2026-01-01')",
            (retired,),
        )
        conn.execute(
            "INSERT INTO provider_items (id, provider_id, native_id, title_as_given, raw_payload,"
            " schema_version, first_seen_at, last_seen_at)"
            " VALUES (1, 'legacy', 'n', 't', '{}', 1, '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO entries (work_id, provider_id, provider_item_id, kind, logged_at,"
            " logged_precision, ingested_at)"
            " VALUES ('w', 'legacy', 1, 'listen', '2026-01-01', 'exact', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO merge_log (id, subject, operation, winner_id, loser_ids, performed_at,"
            " snapshot) VALUES (1, 'work', 'merge', 'w', '[]', '2026-01-01', ?)",
            (json.dumps(snapshot),),
        )
        conn.commit()

    command.upgrade(config, "head")

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT media_type FROM works").fetchall() == [("other",)]
        assert conn.execute("SELECT count(*) FROM entries").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM provider_items").fetchone() == (1,)
        stored_snapshot = json.loads(conn.execute("SELECT snapshot FROM merge_log").fetchone()[0])
    assert stored_snapshot["works"][0]["media_type"] == "other"


def test_0015_preserves_every_work_related_row_during_the_rebuild(tmp_path: Path) -> None:
    """The SQLite CHECK replacement must not cascade-delete any work-owned data."""
    db = tmp_path / "aggregato.db"
    config = _config(_url(db))
    command.upgrade(config, "0014")
    work_id = "00000000000000000000000000000001"
    child_id = "00000000000000000000000000000002"
    untouched_id = "00000000000000000000000000000003"
    creator_id = "00000000000000000000000000000004"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at)"
            " VALUES (?, 'podcast_episode', 'Legacy audio', 'legacy audio',"
            " '2026-01-01', '2026-01-01')",
            (work_id,),
        )
        conn.execute(
            "INSERT INTO works (id, media_type, title, sort_title, parent_work_id,"
            " created_at, updated_at) VALUES (?, 'tv', 'Child', 'child', ?,"
            " '2026-01-01', '2026-01-01')",
            (child_id, work_id),
        )
        conn.execute(
            "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at)"
            " VALUES (?, 'film', 'Untouched', 'untouched', '2026-01-01', '2026-01-01')",
            (untouched_id,),
        )
        conn.execute(
            "INSERT INTO creators (id, kind, name, sort_name, metadata, created_at, updated_at)"
            " VALUES (?, 'person', 'Creator', 'creator', '{}', '2026-01-01', '2026-01-01')",
            (creator_id,),
        )
        conn.execute(
            "INSERT INTO external_ids (id, work_id, namespace, value, source, confidence,"
            " created_at)"
            " VALUES (1, ?, 'fixture', 'legacy-work', 'legacy', 'asserted', '2026-01-01')",
            (work_id,),
        )
        conn.execute(
            "INSERT INTO provider_items (id, provider_id, native_id, work_id, title_as_given,"
            " raw_payload, schema_version, first_seen_at, last_seen_at)"
            " VALUES (1, 'legacy', 'n', ?, 'Legacy audio', '{}', 1, '2026-01-01', '2026-01-01')",
            (work_id,),
        )
        conn.execute(
            "INSERT INTO entries (id, work_id, provider_id, provider_item_id, native_id, kind,"
            " logged_at, logged_precision, ingested_at)"
            " VALUES (1, ?, 'legacy', 1, 'event', 'listen', '2026-01-01', 'exact', '2026-01-01')",
            (work_id,),
        )
        conn.execute(
            "INSERT INTO opinions (id, work_id, provider_id, provider_item_id, updated_at)"
            " VALUES (1, ?, 'legacy', 1, '2026-01-01')",
            (work_id,),
        )
        conn.execute(
            "INSERT INTO work_credits (id, work_id, creator_id, role, role_raw, position,"
            " source, link_confidence) VALUES (1, ?, ?, 'author', 'Host', 0, 'legacy', 'asserted')",
            (work_id, creator_id),
        )
        conn.execute(
            "INSERT INTO resolution_queue (id, subject, provider_id, payload_ref, candidates,"
            " proposed, created_at) VALUES (1, 'work', 'legacy', 1, '[]', '{}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO merge_log (id, subject, operation, winner_id, loser_ids, performed_at,"
            " snapshot) VALUES (1, 'work', 'merge', ?, '[]', '2026-01-01', ?)",
            (
                work_id,
                json.dumps({"works": [{"id": work_id, "media_type": "podcast_episode"}]}),
            ),
        )
        conn.execute(
            "INSERT INTO search_index (kind, ref_id, content) VALUES ('work_title', ?,"
            " 'Legacy audio')",
            (work_id,),
        )
        before = {
            "works": conn.execute("SELECT * FROM works ORDER BY id").fetchall(),
            "external_ids": conn.execute("SELECT * FROM external_ids ORDER BY id").fetchall(),
            "provider_items": conn.execute("SELECT * FROM provider_items ORDER BY id").fetchall(),
            "entries": conn.execute("SELECT * FROM entries ORDER BY id").fetchall(),
            "opinions": conn.execute("SELECT * FROM opinions ORDER BY id").fetchall(),
            "work_credits": conn.execute("SELECT * FROM work_credits ORDER BY id").fetchall(),
            "resolution_queue": conn.execute(
                "SELECT * FROM resolution_queue ORDER BY id"
            ).fetchall(),
            "search_index": conn.execute(
                "SELECT kind, ref_id, content FROM search_index ORDER BY kind, ref_id"
            ).fetchall(),
        }
        conn.commit()

    command.upgrade(config, "head")

    with closing(sqlite3.connect(db)) as conn:
        after_works = conn.execute("SELECT * FROM works ORDER BY id").fetchall()
        expected_works = [
            (*row[:1], "other" if row[1] == "podcast_episode" else row[1], *row[2:])
            for row in before["works"]
        ]
        assert after_works == expected_works
        for statement, table in (
            ("SELECT * FROM external_ids ORDER BY id", "external_ids"),
            ("SELECT * FROM provider_items ORDER BY id", "provider_items"),
            ("SELECT * FROM entries ORDER BY id", "entries"),
            ("SELECT * FROM opinions ORDER BY id", "opinions"),
            ("SELECT * FROM resolution_queue ORDER BY id", "resolution_queue"),
        ):
            assert conn.execute(statement).fetchall() == before[table]
        assert (
            conn.execute(
                "SELECT id, work_id, creator_id, role, role_raw, credited_as, position, source, "
                "link_confidence FROM work_credits ORDER BY id"
            ).fetchall()
            == before["work_credits"]
        )
        assert conn.execute(
            "SELECT manual_from_creator_id FROM work_credits ORDER BY id"
        ).fetchall() == [(None,)]
        assert (
            conn.execute(
                "SELECT kind, ref_id, content FROM search_index ORDER BY kind, ref_id"
            ).fetchall()
            == before["search_index"]
        )
        snapshot = json.loads(conn.execute("SELECT snapshot FROM merge_log").fetchone()[0])
        assert snapshot == {"works": [{"id": work_id, "media_type": "other"}]}


def test_0016_preserves_existing_sync_history_and_backfills_progress_defaults(
    tmp_path: Path,
) -> None:
    """The live-progress columns are additive to runs already recorded by older versions."""
    db = tmp_path / "aggregato.db"
    config = _config(_url(db))
    command.upgrade(config, "0015")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO sync_runs "
            "(id, provider_id, lineage_id, attempt, mode, status, started_at, items_seen, "
            "items_written, items_failed, error_message) "
            "VALUES (7, 'legacy', ?, 2, 'incremental', 'success', '2026-01-01', 4, 3, 1, "
            "'retained')",
            ("7" * 32,),
        )
        conn.execute(
            "INSERT INTO ingest_failures "
            "(id, provider_id, sync_run_id, raw_payload, error, stage, created_at) "
            "VALUES (8, 'legacy', 7, ?, 'retained failure', 'normalize', '2026-01-01')",
            (json.dumps({"id": "payload"}),),
        )
        conn.commit()

    command.upgrade(config, "head")

    with closing(sqlite3.connect(db)) as conn:
        row = conn.execute(
            "SELECT provider_id, lineage_id, attempt, mode, status, items_seen, items_written, "
            "items_failed, error_message, phase, progress_total, checkpoint_count, "
            "progress_revision, updated_at FROM sync_runs WHERE id = 7"
        ).fetchone()
        failure = conn.execute(
            "SELECT sync_run_id, raw_payload, error FROM ingest_failures WHERE id = 8"
        ).fetchone()

    assert row == (
        "legacy",
        "7" * 32,
        2,
        "incremental",
        "success",
        4,
        3,
        1,
        "retained",
        "finished",
        None,
        0,
        0,
        "2026-01-01",
    )
    assert failure == (7, json.dumps({"id": "payload"}), "retained failure")


def test_0018_preserves_provider_state_and_initializes_now_playing_fields(
    tmp_path: Path,
) -> None:
    """Current-item state is additive and leaves the existing scheduler state untouched."""
    db = tmp_path / "aggregato.db"
    config = _config(_url(db))
    command.upgrade(config, "0017")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO providers "
            "(id, enabled, status, acquisition, schema_version, reviewed, config, created_at, "
            "updated_at) VALUES ('legacy', 1, 'idle', 'api', 3, 1, ?, '2026-01-01', "
            "'2026-01-02')",
            (json.dumps({"username": "old"}),),
        )
        conn.execute(
            "INSERT INTO provider_state "
            "(provider_id, cursor, next_run_at, effective_interval_seconds, "
            "consecutive_failures, retry_step, last_success_at, requested_mode, "
            "requested_lineage_id, last_window_item_count, last_failed_window_item_count, kv) "
            "VALUES ('legacy', ?, '2026-01-03', 3600, 2, 1, '2026-01-04', 'full', ?, 17, 4, ?)",
            (
                json.dumps({"cursor": "old"}),
                "8" * 32,
                json.dumps({"provider": "state"}),
            ),
        )
        before = conn.execute(
            "SELECT provider_id, cursor, next_run_at, effective_interval_seconds, "
            "consecutive_failures, retry_step, last_success_at, requested_mode, "
            "requested_lineage_id, last_window_item_count, last_failed_window_item_count, kv "
            "FROM provider_state WHERE provider_id = 'legacy'"
        ).fetchone()
        conn.commit()

    command.upgrade(config, "head")

    with closing(sqlite3.connect(db)) as conn:
        after = conn.execute(
            "SELECT provider_id, cursor, next_run_at, effective_interval_seconds, "
            "consecutive_failures, retry_step, last_success_at, requested_mode, "
            "requested_lineage_id, last_window_item_count, last_failed_window_item_count, kv, "
            "now_playing_item, now_playing_changed_at, now_playing_checked_at, "
            "now_playing_next_poll_at, now_playing_failures, now_playing_config_fingerprint "
            "FROM provider_state WHERE provider_id = 'legacy'"
        ).fetchone()

    assert after[:12] == before
    assert after[12:] == (None, None, None, None, 0, None)


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


def test_0009_preserves_existing_failures_when_adding_native_identity(
    tmp_path: Path,
) -> None:
    """The envelope column is additive; old captured payloads must survive the rebuild."""
    db = tmp_path / "aggregato.db"
    config = _config(_url(db))
    command.upgrade(config, "0008")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO sync_runs "
            "(id, provider_id, lineage_id, attempt, mode, status, started_at) "
            "VALUES (1, 'test', ?, 1, 'incremental', 'running', '2026-01-01')",
            ("1" * 32,),
        )
        conn.execute(
            "INSERT INTO ingest_failures "
            "(provider_id, sync_run_id, raw_payload, error, stage, created_at) "
            "VALUES ('test', 1, ?, 'bad', 'normalize', '2026-01-01')",
            (json.dumps({"id": "payload-only"}),),
        )
        conn.commit()

    command.upgrade(config, "head")

    with closing(sqlite3.connect(db)) as conn:
        row = conn.execute(
            "SELECT native_id, raw_payload FROM ingest_failures WHERE id = 1"
        ).fetchone()
    assert row[0] is None
    assert json.loads(row[1]) == {"id": "payload-only"}


def test_0019_preserves_credits_and_backfills_active_manual_splits(tmp_path: Path) -> None:
    db = tmp_path / "aggregato.db"
    config = _config(_url(db))
    command.upgrade(config, "0018")
    source, target, work, survivor, corrected = (
        "1" * 32,
        "2" * 32,
        "3" * 32,
        "4" * 32,
        "5" * 32,
    )
    source_json = str(uuid.UUID(hex=source))
    target_json = str(uuid.UUID(hex=target))
    snapshot = {
        "work_credits": [
            {
                "id": 11,
                "work_id": str(uuid.UUID(hex=work)),
                "creator_id": source_json,
                "role": "performer",
                "role_raw": "artists",
                "credited_as": None,
                "position": 0,
                "source": "fixture",
                "link_confidence": "matched",
            }
        ]
    }
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO creators (id, kind, name, sort_name, metadata, created_at, updated_at)"
            " VALUES (?, 'person', 'Source', 'source', '{}', '2026-01-01', '2026-01-01')",
            (source,),
        )
        conn.execute(
            "INSERT INTO creators (id, kind, name, sort_name, metadata, created_at, updated_at)"
            " VALUES (?, 'person', 'Target', 'target', '{}', '2026-01-01', '2026-01-01')",
            (target,),
        )
        conn.execute(
            "INSERT INTO creators (id, kind, name, sort_name, metadata, created_at, updated_at)"
            " VALUES (?, 'person', 'Survivor', 'survivor', '{}', '2026-01-01', '2026-01-01')",
            (survivor,),
        )
        conn.execute(
            "INSERT INTO creators (id, kind, name, sort_name, metadata, created_at, updated_at)"
            " VALUES (?, 'person', 'Corrected', 'corrected', '{}', '2026-01-01', '2026-01-01')",
            (corrected,),
        )
        conn.execute(
            "INSERT INTO works (id, media_type, title, sort_title, metadata, created_at,"
            " updated_at)"
            " VALUES (?, 'track', 'Track', 'track', '{}', '2026-01-01', '2026-01-01')",
            (work,),
        )
        conn.execute(
            "INSERT INTO work_credits (id, work_id, creator_id, role, role_raw, position, source,"
            " link_confidence) VALUES (11, ?, ?, 'performer', 'artists', 0, 'fixture', 'matched')",
            (work, corrected),
        )
        conn.execute(
            "INSERT INTO work_credits (id, work_id, creator_id, role, role_raw, position, source,"
            " link_confidence) VALUES (12, ?, ?, 'author', 'writers', 1, 'fixture', 'matched')",
            (work, survivor),
        )
        conn.execute(
            "INSERT INTO merge_log (id, subject, operation, winner_id, loser_ids, moved_credit_ids,"
            " performed_at, snapshot) VALUES (1, 'creator', 'split', ?, ?, '[11]',"
            " '2026-01-01', ?)",
            (target, json.dumps([source_json]), json.dumps(snapshot)),
        )
        stale_snapshot = {
            "work_credits": [
                {
                    **snapshot["work_credits"][0],
                    "id": 12,
                    "role": "author",
                    "role_raw": "writers",
                    "position": 1,
                }
            ]
        }
        conn.execute(
            "INSERT INTO merge_log (id, subject, operation, winner_id, loser_ids,"
            " moved_credit_ids, performed_at, snapshot) VALUES (3, 'creator', 'split', ?, ?,"
            " '[12]', '2026-01-03', ?)",
            (target, json.dumps([source_json]), json.dumps(stale_snapshot)),
        )
        conn.execute(
            "INSERT INTO merge_log (id, subject, operation, winner_id, loser_ids, performed_at,"
            " snapshot) VALUES (2, 'creator', 'merge', ?, ?, '2026-01-02', '{}')",
            (survivor, json.dumps([source_json])),
        )
        conn.execute(
            "INSERT INTO merge_log (id, subject, operation, winner_id, loser_ids, performed_at,"
            " snapshot) VALUES (4, 'creator', 'merge', ?, ?, '2026-01-04', '{}')",
            (corrected, json.dumps([target_json])),
        )
        conn.execute("DELETE FROM creators WHERE id=?", (source,))
        conn.execute("DELETE FROM creators WHERE id=?", (target,))
        conn.commit()

    command.upgrade(config, "head")

    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute(
            "SELECT work_id, creator_id, source, link_confidence, manual_from_creator_id "
            "FROM work_credits WHERE id=11"
        ).fetchone() == (work, corrected, "fixture", "manual", source)
        assert conn.execute(
            "SELECT creator_id, link_confidence, manual_from_creator_id FROM work_credits "
            "WHERE id=12"
        ).fetchone() == (survivor, "matched", None)
        assert conn.execute("SELECT id FROM creators ORDER BY id").fetchall() == [
            (survivor,),
            (corrected,),
        ]
        assert conn.execute("SELECT id FROM merge_log ORDER BY id").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not any(
            row[3] == "manual_from_creator_id"
            for row in conn.execute("PRAGMA foreign_key_list(work_credits)")
        )
        conn.execute("UPDATE work_credits SET manual_from_creator_id=? WHERE id=11", ("5" * 32,))
