"""Run the database-backed scheduler as a separate worker process.

The worker owns migrations, interrupted-run recovery, scheduling, and isolated provider children.
The API remains available while a provider run is slow or fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import signal
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config, ConfigError, load_config
from aggregato.db.engine import create_engine
from aggregato.db.migrate import upgrade_to_head
from aggregato.db.retention import cleanup
from aggregato.domain.clock import SYSTEM_CLOCK, Clock
from aggregato.logging import configure_logging
from aggregato.sync.dispatch import build_dispatch
from aggregato.sync.now_playing import NowPlayingMonitor
from aggregato.sync.scheduler import Scheduler, max_concurrent_runs, recover_interrupted_runs

log = logging.getLogger(__name__)
_WORKER_LOCK_KEY = 482901737


@contextmanager
def _sqlite_worker_lock(database_url: str) -> Iterator[bool]:
    url = make_url(database_url)
    # ponytail: one worker per SQLite file; durable ownership is for multi-host workers.
    database = url.database
    if not database or database == ":memory:" or url.query.get("mode") == "memory":
        raise RuntimeError("the scheduler worker requires file-backed SQLite")
    path = Path(database).resolve()
    lock_path = path.with_name(path.name + ".worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def single_worker_lock(engine: AsyncEngine, database_url: str) -> AsyncIterator[bool]:
    """Hold the process-lifetime scheduler lock for SQLite or PostgreSQL."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        with _sqlite_worker_lock(database_url) as acquired:
            yield acquired
        return

    if url.get_backend_name() != "postgresql":
        raise RuntimeError("the scheduler singleton lock supports SQLite and PostgreSQL")

    async with engine.connect() as connection:
        acquired = False
        try:
            acquired = bool(
                (
                    await connection.execute(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": _WORKER_LOCK_KEY},
                    )
                ).scalar_one()
            )
            await connection.commit()
            yield acquired
        finally:
            if acquired:
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": _WORKER_LOCK_KEY},
                    )
                    await connection.commit()
                except BaseException:
                    await connection.invalidate()
                    raise


async def serve(config: Config, *, clock: Clock = SYSTEM_CLOCK) -> None:
    """Run the scheduler until stopped.

    Args:
        config: Loaded configuration. The token is not needed here — the scheduler serves no HTTP —
            but ``load_config`` enforces it anyway, so a misconfigured install fails the same way in
            both processes rather than half-starting.
    """
    engine = create_engine(config.database_url)
    try:
        async with single_worker_lock(engine, config.database_url) as acquired:
            if not acquired:
                raise RuntimeError("another scheduler worker already owns this database")

            # The worker may start before the API, so it applies migrations under its lock.
            # SQLite and Alembic's Postgres env also serialize migration with the API process.
            await asyncio.to_thread(upgrade_to_head, config.database_url)

            # Recovery is safe only after this process owns the scheduler singleton.
            recovered = await recover_interrupted_runs(engine, now=clock.now())
            if recovered:
                log.warning("recovered interrupted syncs for %s", ", ".join(recovered))
            scheduler = Scheduler(
                engine,
                clock=clock,
                dispatch=build_dispatch(engine, config, clock=clock),
                max_concurrent=max_concurrent_runs(config.database_url),
                provider_dir=config.provider_dir,
            )
            retention_task = asyncio.create_task(
                _retention_loop(engine, config.data_dir, clock=clock), name="retention-cleanup"
            )
            now_playing = NowPlayingMonitor(engine, config, clock=clock)
            now_playing_task = asyncio.create_task(
                now_playing.run_forever(), name="now-playing-monitor"
            )

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, scheduler.request_stop)

            try:
                await scheduler.run_forever()
            finally:
                scheduler.request_stop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    with contextlib.suppress(NotImplementedError):
                        loop.remove_signal_handler(sig)
                retention_task.cancel()
                await asyncio.gather(
                    scheduler.stop(),
                    now_playing.stop(),
                    now_playing_task,
                    retention_task,
                    return_exceptions=True,
                )
    finally:
        await engine.dispose()
        log.info("scheduler stopped")


async def _retention_loop(
    engine: AsyncEngine, data_dir: Path, *, clock: Clock = SYSTEM_CLOCK
) -> None:
    """Run retention cleanup at boot and daily; it remains independent of provider runs."""
    while True:
        try:
            await cleanup(engine, data_dir, now=clock.now())
        except Exception:
            log.exception("retention cleanup failed")
        await asyncio.sleep(24 * 60 * 60)


def main(argv: list[str] | None = None) -> int:
    """Load configuration, then serve.

    Returns:
        A process exit code. A configuration error exits 2 with the reason on stderr rather than a
        traceback: the operator's next action is to fix a setting, not to read a stack.
    """
    del argv  # no flags; everything comes from the environment and the config file
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        # print, not log: this runs before logging is useful, and the operator's next action is
        # to fix a setting rather than to read a stack.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    asyncio.run(serve(config))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())
