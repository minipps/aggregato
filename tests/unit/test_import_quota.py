"""Focused coverage for the cross-provider import quota lock."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aggregato.api.routes.providers import _lock_import_quota


@pytest.mark.asyncio
async def test_postgres_total_quota_uses_a_transaction_advisory_lock() -> None:
    connection = AsyncMock()
    connection.dialect = SimpleNamespace(name="postgresql")

    await _lock_import_quota(connection)

    statement = connection.execute.await_args.args[0]
    assert "pg_advisory_xact_lock" in str(statement)
    assert connection.execute.await_args.args[1]["lock_key"] == 482901736


@pytest.mark.asyncio
async def test_sqlite_total_quota_uses_the_existing_writer_transaction_lock() -> None:
    connection = AsyncMock()
    connection.dialect = SimpleNamespace(name="sqlite")

    await _lock_import_quota(connection)

    connection.execute.assert_not_awaited()
