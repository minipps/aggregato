"""Shared SQLAlchemy types for the supported SQLite and PostgreSQL databases."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Numeric, Uuid
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.types import TypeEngine

#: UUID primary keys: native ``uuid`` on Postgres, ``CHAR(32)`` on SQLite.
UUID_PK = Uuid(as_uuid=True)

#: Auto-increment keys. On SQLite the ``INTEGER`` variant matters: only that exact type aliases
#: rowid, and a ``BIGINT`` primary key silently stops auto-incrementing.
AUTO_PK: TypeEngine[int] = BigInteger().with_variant(sqlite.INTEGER(), "sqlite")

#: Foreign keys pointing at an ``AUTO_PK``. Same variant, so join types match on both dialects.
AUTO_FK: TypeEngine[int] = BigInteger().with_variant(sqlite.INTEGER(), "sqlite")

#: Timestamps represent UTC instants. SQLite returns them without timezone metadata, so readers
#: interpret its naive values as UTC.
TIMESTAMP = DateTime(timezone=True)

#: JSON with a ``JSONB`` variant on Postgres, where it is both smaller and indexable.
#:
#: Store Python ``None`` as SQL NULL, not the JSON value ``null``. Whole-work queries rely on
#: ``subject_ref IS NULL``; JSON null would not match that predicate. Fallback event upserts use the
#: separate canonical ``subject_ref_key`` column because JSON is not a portable conflict target.
JSON_COL: TypeEngine[Any] = JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)

#: Ratings and progress values use Decimal to avoid binary floating-point rounding between steps.
DECIMAL = Numeric(10, 4, asdecimal=True)
