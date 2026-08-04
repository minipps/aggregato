"""The async engine (research.md ).

SQLAlchemy Core only — no ORM, no session, no repository layer. A ``Connection`` *is* the interface
the writers and readers take, so the surface here is deliberately two functions: build an engine
from a URL, and run a block inside one transaction.

The same URL setting selects the dialect : ``sqlite+aiosqlite:///…`` for the default
single-container install, ``postgresql+asyncpg://…`` past ~1M entries, with no other code change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

#: How long SQLite waits on a locked database before raising. WAL keeps readers off the writer's
#: back, but two writers (the API and a sync run) still serialize, and the loser must wait rather
#: than fail — a few seconds covers a batch commit.
SQLITE_BUSY_TIMEOUT_MS = 5000


def create_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build the async engine for ``url``.

    Inputs: a SQLAlchemy URL string owned by the caller (config lives in ``aggregato.config``;
    this module reads no environment). ``echo`` logs emitted SQL.

    On SQLite the per-connection PRAGMAs below are attached as a ``connect`` listener so every
    pooled connection gets them, not just the first.

    Failure modes: an unparseable or unknown-driver URL raises ``sqlalchemy.exc.ArgumentError``
    immediately; a missing driver package raises ``ModuleNotFoundError``. Connecting is lazy, so a
    bad host or path surfaces on first use, not here.
    """
    engine = create_async_engine(url, echo=echo)
    if make_url(url).get_backend_name() == "sqlite":
        _apply_sqlite_pragmas(engine)
    return engine


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Attach the SQLite session PRAGMAs to every connection this engine opens."""

    # The listener runs on the sync DBAPI connection underneath aiosqlite, which is why it is
    # registered against engine.sync_engine and uses a plain cursor.
    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            # ON is not the SQLite default, so without this the schema's ondelete rules are
            # decorative and orphan rows get written silently.
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL: a sync run's writes must not block the API's reads .
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()


@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Yield a connection inside one transaction, committing on exit and rolling back on error.

    Inputs: the engine. Yields an ``AsyncConnection`` — pass it straight to ``conn.execute(...)``.

    Failure modes: any exception raised inside the block rolls the whole transaction back and
    propagates. Nothing is retried here; the retry ladder is the sync layer's job.
    """
    async with engine.begin() as conn:
        yield conn
