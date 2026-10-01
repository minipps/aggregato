"""The provider-item index migration preserves existing entries and enables indexed lookups."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from aggregato.db import migrate
from aggregato.db.schema import entries
from aggregato.db.search import review_condition


def test_0020_indexes_existing_entries_without_losing_them(tmp_path: Path) -> None:
    database = tmp_path / "aggregato.db"
    config = Config()
    config.set_main_option("script_location", str(migrate._SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database}")
    command.upgrade(config, "0019")

    with closing(sqlite3.connect(database)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO works (id, media_type, title, sort_title, created_at, updated_at) "
            "VALUES ('w', 'film', 'Work', 'work', '2026-01-01', '2026-01-01')"
        )
        conn.executemany(
            "INSERT INTO provider_items (id, provider_id, native_id, work_id, title_as_given, "
            "raw_payload, schema_version, first_seen_at, last_seen_at) "
            "VALUES (?, 'fixture', ?, 'w', 'Work', '{}', 1, '2026-01-01', '2026-01-01')",
            [(number, f"item-{number}") for number in range(1, 101)],
        )
        conn.executemany(
            "INSERT INTO entries (work_id, provider_id, provider_item_id, native_id, kind, "
            "logged_at, logged_precision, ingested_at) "
            "VALUES ('w', 'fixture', ?, ?, 'watch', '2026-01-01 12:00:00', 'exact', "
            "'2026-01-01 12:00:00')",
            [
                (provider_item_id, f"event-{provider_item_id}-{event}")
                for provider_item_id in range(1, 101)
                for event in range(10)
            ],
        )
        conn.commit()

    command.upgrade(config, "head")

    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT count(*) FROM entries").fetchone() == (1_000,)
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index' "
            "AND name='ix_entries_provider_item_id'"
        ).fetchone() == (1,)
        conn.execute(
            "INSERT INTO search_index (kind, ref_id, content) "
            "VALUES ('review_text', '1:0', 'needle')"
        )
        conn.execute("ANALYZE")
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM entries WHERE provider_item_id=1"
        ).fetchall()
        review_sql = (
            select(entries.c.id)
            .where(review_condition("sqlite", entries.c.provider_item_id, "needle"))
            .compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
        )
        review_plan = conn.execute(f"EXPLAIN QUERY PLAN {review_sql}").fetchall()
    assert any("ix_entries_provider_item_id" in row[3] for row in plan)
    assert any("ix_entries_provider_item_id" in row[3] for row in review_plan)
