"""The portability rules, in one place (research.md ).

Both dialects stay first-class , so every type choice that differs between SQLite and
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
#:
#: ``none_as_null=True`` is not a preference — it is load-bearing. By default SQLAlchemy's JSON type
#: stores Python ``None`` as the JSON text ``'null'``, which is a *value*, so ``col IS NULL`` is
#: false for it. Every aggregate in this system filters ``subject_ref IS NULL`` to exclude sub-unit
#: records (, research.md ), and the writer deduplicates unidentified entries on a
#: ``subject_ref`` comparison that includes the null case. With the default, both would silently
#: stop working: statistics would count episodes as whole works, and a feed without event ids would
#: duplicate its history on every resync. Neither failure announces itself.
JSON_COL: TypeEngine[Any] = JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)

#: Ratings and progress values. Never float: a 0.5-step scale compared against a float is how a
#: rating lands "between steps" for no reason a user could explain.
DECIMAL = Numeric(10, 4, asdecimal=True)
