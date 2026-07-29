"""The portability rules, in one place (research.md R4).

Both dialects stay first-class (FR-048), so every type choice that differs between SQLite and
Postgres is made once here rather than per column. A schema that reaches for a Postgres-only type
is a schema the default install cannot run.
"""

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

#: Every timestamp is timezone-aware and **stored UTC**, converted at the API edge only. SQLite has
#: no timestamp type, so this is the one place the aware/naive boundary is decided.
TIMESTAMP = DateTime(timezone=True)

#: JSON with a ``JSONB`` variant on Postgres, where it is both smaller and indexable.
JSON_COL: TypeEngine[Any] = JSON().with_variant(postgresql.JSONB(), "postgresql")

#: Ratings and progress values. Never float: a 0.5-step scale compared against a float is how a
#: rating lands "between steps" for no reason a user could explain.
DECIMAL = Numeric(10, 4, asdecimal=True)
