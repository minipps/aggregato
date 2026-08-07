"""The schema must stay first-class on both dialects (, research.md ).

These are cheap compile-time checks rather than round-trips against a server, and they catch the
class of mistake that is otherwise found only by a self-hoster running the non-default dialect:
a Postgres-only type, a native enum, a partial index one dialect silently drops.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from aggregato.db.schema import entries, metadata, provider_items, providers

DIALECTS: list[tuple[str, Dialect]] = [
    ("sqlite", sqlite.dialect()),
    ("postgresql", postgresql.dialect()),
]


@pytest.mark.parametrize(("name", "dialect"), DIALECTS)
def test_every_table_compiles(name: str, dialect: Dialect) -> None:
    for table in metadata.sorted_tables:
        CreateTable(table).compile(dialect=dialect)


@pytest.mark.parametrize(("name", "dialect"), DIALECTS)
def test_every_index_compiles(name: str, dialect: Dialect) -> None:
    for table in metadata.sorted_tables:
        for index in table.indexes:
            CreateIndex(index).compile(dialect=dialect)


def test_no_native_database_enums() -> None:
    """Every vocabulary is text plus a CHECK, never a native enum.

    A native enum is the trap: each added ``media_type`` becomes a Postgres ``ALTER TYPE`` with no
    SQLite equivalent, whereas a text column plus CHECK migrates identically on both.
    """
    offenders = [
        f"{table.name}.{column.name}"
        for table in metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, SAEnum)
    ]
    assert offenders == [], f"native database enums found: {offenders}"


def test_boolean_defaults_compile_for_both_dialects() -> None:
    """Fresh PostgreSQL installs must not receive SQLite's integer boolean literals."""
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        compiled = str(CreateTable(providers).compile(dialect=dialect)).lower()
        assert "enabled boolean" in compiled
        assert "reviewed boolean" in compiled
        if dialect.name == "postgresql":
            assert "default false" in compiled
            assert "default true" in compiled
        else:
            assert "default 0" in compiled
            assert "default 1" in compiled


def test_no_naive_timestamp_columns() -> None:
    """Every timestamp is timezone-aware, because everything is stored UTC (data-model.md)."""
    naive = [
        f"{table.name}.{column.name}"
        for table in metadata.sorted_tables
        for column in table.columns
        if column.type.__class__.__name__ == "DateTime"
        and not getattr(column.type, "timezone", False)
    ]
    assert naive == [], f"naive timestamp columns: {naive}"


def test_entries_partial_unique_index_is_emitted_by_both_dialects() -> None:
    """The idempotency key for identified events, and the reason it must be partial.

    Entries whose platform gives the event no id carry ``native_id IS NULL``. A plain unique index
    would make two such nulls collide on Postgres semantics or not at all on others; the partial
    predicate is what keeps unidentified events out of the constraint entirely, leaving them to the
    writer's ``(provider_item_id, kind, logged_at, subject_ref)`` fallback.
    """
    index = next(i for i in entries.indexes if i.name == "uq_entries_provider_id_native_id")
    assert index.unique
    for _, dialect in DIALECTS:
        sql = str(CreateIndex(index).compile(dialect=dialect))
        assert "WHERE native_id IS NOT NULL" in sql, sql


def test_provider_items_idempotency_key_exists() -> None:
    """rests on this one constraint: a resync writes nothing new because of it."""
    keys = {
        tuple(c.name for c in constraint.columns)
        for constraint in provider_items.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("provider_id", "native_id") in keys


def test_no_user_id_column_anywhere() -> None:
    """— single-user by construction. Adding one is a v2 schema break, accepted knowingly."""
    offenders = [
        f"{table.name}.{column.name}"
        for table in metadata.sorted_tables
        for column in table.columns
        if column.name in {"user_id", "owner_id", "account_id"}
    ]
    assert offenders == [], f"multi-user columns crept in: {offenders}"


def test_sqlite_autoincrement_keys_use_the_integer_variant() -> None:
    """Only ``INTEGER PRIMARY KEY`` aliases rowid on SQLite.

    A ``BIGINT`` primary key compiles fine and then silently stops auto-incrementing, which shows up
    much later as an integrity error on the second insert.
    """
    dialect = sqlite.dialect()
    for table in metadata.sorted_tables:
        for column in table.primary_key.columns:
            if not column.autoincrement or column.autoincrement == "auto":
                continue
            rendered = column.type.dialect_impl(dialect).compile(dialect=dialect)
            assert rendered == "INTEGER", f"{table.name}.{column.name} renders as {rendered}"


async def test_a_null_json_column_stores_sql_null_not_the_json_text_null() -> None:
    """SQLAlchemy's JSON type maps Python ``None`` to the JSON text ``'null'`` by DEFAULT.

    That default is silently catastrophic here, which is why ``JSON_COL`` sets
    ``none_as_null=True`` and why this test exists rather than a comment.

    ``'null'`` is a JSON *value*, so ``subject_ref IS NULL`` is FALSE for it. Two things break at
    once and neither announces itself:

    * every aggregate filters ``subject_ref IS NULL`` to exclude sub-unit records (,
      research.md ), so per-episode rows would start counting as whole works — the exact silent
      statistic corruption the spec calls out;
    * the writer deduplicates entries with no native id on a ``subject_ref`` comparison including
      the null case, so a feed without event ids would duplicate its entire history on every
      resync .
    """
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from aggregato.db.engine import create_engine
    from aggregato.db.schema import provider_items, works

    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            work_id = uuid.uuid4()
            await conn.execute(
                works.insert().values(
                    id=work_id,
                    media_type="film",
                    title="X",
                    sort_title="x",
                    metadata={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await conn.execute(
                provider_items.insert().values(
                    provider_id="p",
                    native_id="n",
                    work_id=work_id,
                    title_as_given="X",
                    raw_payload={},
                    schema_version=1,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            item_id = (await conn.execute(select(provider_items.c.id))).scalar_one()
            await conn.execute(
                entries.insert().values(
                    work_id=work_id,
                    provider_id="p",
                    provider_item_id=item_id,
                    native_id=None,
                    kind="watch",
                    logged_at=now,
                    logged_precision="exact",
                    subject_ref=None,
                    metadata={},
                    ingested_at=now,
                )
            )

            is_null = await conn.execute(
                select(func.count()).select_from(entries).where(entries.c.subject_ref.is_(None))
            )
            assert is_null.scalar_one() == 1, (
                "subject_ref stored the JSON text 'null' instead of SQL NULL; every "
                "subject_ref IS NULL aggregate and the no-native-id dedup are now broken"
            )
    finally:
        await engine.dispose()
