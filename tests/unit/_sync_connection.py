"""A tiny awaitable adapter for SQL-focused unit tests.

Resolver tests exercise SQL construction and identity rules.  They do not need the aiosqlite
worker thread, which is unavailable in the sandbox runner, so they use a synchronous SQLite
connection behind the same awaitable ``execute`` surface their production code consumes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Connection


class SyncConnectionAdapter:
    """Expose the subset of ``AsyncConnection`` used by isolated resolver tests."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    @property
    def dialect(self) -> Any:
        """Expose the dialect used by the shared upsert statement builder."""
        return self._connection.dialect

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute synchronously behind the production awaitable interface."""
        return self._connection.execute(statement, *args, **kwargs)
