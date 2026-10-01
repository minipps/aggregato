"""Worker process lifetime ownership."""

from pathlib import Path

import pytest

from aggregato.db.engine import create_engine
from aggregato.worker import single_worker_lock


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar_one(self) -> bool:
        return self.value


class _PostgresEngine:
    def __init__(self) -> None:
        self.locked = False
        self.connections: list[_PostgresConnection] = []

    def connect(self) -> "_PostgresConnection":
        connection = _PostgresConnection(self)
        self.connections.append(connection)
        return connection


class _PostgresConnection:
    def __init__(self, engine: _PostgresEngine) -> None:
        self.engine = engine
        self.commits = 0
        self.closed = False
        self.statements: list[str] = []

    async def __aenter__(self) -> "_PostgresConnection":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def execute(self, statement: object, parameters: object = None) -> _ScalarResult:
        sql = str(statement)
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            acquired = not self.engine.locked
            self.engine.locked = self.engine.locked or acquired
            return _ScalarResult(acquired)
        if "pg_advisory_unlock" in sql:
            self.engine.locked = False
            return _ScalarResult(True)
        raise AssertionError(f"unexpected PostgreSQL lock SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1

    async def invalidate(self) -> None:
        self.engine.locked = False


async def test_sqlite_worker_lock_refuses_overlap_and_releases_on_startup_error(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}"
    engine = create_engine(database_url)
    try:
        async with single_worker_lock(engine, database_url) as owner:
            assert owner
            async with single_worker_lock(engine, database_url) as second:
                assert not second
        with pytest.raises(RuntimeError, match="startup failed"):
            async with single_worker_lock(engine, database_url):
                raise RuntimeError("startup failed")
        async with single_worker_lock(engine, database_url) as restarted:
            assert restarted
    finally:
        await engine.dispose()


async def test_postgres_worker_lock_refuses_overlap_and_releases_after_error() -> None:
    database_url = "postgresql+asyncpg://user:pass@localhost/archive"
    engine = _PostgresEngine()
    async with single_worker_lock(engine, database_url):  # type: ignore[arg-type]
        assert engine.locked
        async with single_worker_lock(engine, database_url) as second:  # type: ignore[arg-type]
            assert not second

    assert not engine.locked
    with pytest.raises(RuntimeError, match="startup failed"):
        async with single_worker_lock(engine, database_url):  # type: ignore[arg-type]
            raise RuntimeError("startup failed")
    assert not engine.locked
    assert all(connection.closed for connection in engine.connections)
    assert [connection.commits for connection in engine.connections] == [2, 1, 2]


@pytest.mark.parametrize(
    "database_url",
    (
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///file:worker?mode=memory&cache=shared",
    ),
)
async def test_sqlite_worker_lock_rejects_memory_databases(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with pytest.raises(RuntimeError, match="file-backed SQLite"):
            async with single_worker_lock(engine, database_url):
                pytest.fail("memory database unexpectedly acquired a worker lock")
    finally:
        await engine.dispose()
