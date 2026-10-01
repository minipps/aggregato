from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import URL

from aggregato.api.routes.settings import _database_size, _payload_byte_length
from aggregato.db.schema import provider_items
from aggregato.export import sqlite_database_path


def test_storage_size_counts_utf8_bytes_in_sqlite() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        payload = '{"title":"café"}'
        size = conn.scalar(select(_payload_byte_length(literal(payload), "sqlite")))
    engine.dispose()

    assert size == len(payload.encode("utf-8"))


def test_postgres_storage_size_casts_jsonb_to_text_before_counting_bytes() -> None:
    sql = str(
        select(_payload_byte_length(provider_items.c.raw_payload, "postgresql")).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "octet_length(CAST(provider_items.raw_payload AS TEXT))" in sql


def test_database_size_uses_configured_sqlite_file_and_sidecars_only(tmp_path: Path) -> None:
    database = tmp_path / "custom.sqlite"
    database.write_bytes(b"database")
    Path(f"{database}-wal").write_bytes(b"wal")
    Path(f"{database}-shm").write_bytes(b"shm")
    Path(f"{database}.bak").write_bytes(b"backup file must not count")
    (tmp_path / "aggregato.db").write_bytes(b"a different database")
    database_url = URL.create("sqlite+aiosqlite", database=str(database)).render_as_string(
        hide_password=False
    )

    path = sqlite_database_path(database_url)

    assert path == database
    assert _database_size(path) == len(b"databasewalshm")


def test_non_file_database_urls_have_no_backup_or_local_file_size() -> None:
    memory = sqlite_database_path("sqlite+aiosqlite:///:memory:")
    shared_memory = sqlite_database_path("sqlite+aiosqlite:///file:worker?mode=memory&cache=shared")
    postgres = sqlite_database_path("postgresql+asyncpg://user:pass@localhost/archive")

    assert memory is None
    assert shared_memory is None
    assert postgres is None
    assert _database_size(memory) == 0
    assert _database_size(postgres) == 0
