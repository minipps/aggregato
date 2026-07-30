"""The scheduler process entrypoint: ``python -m aggregato.worker``.

A separate process from the API, deliberately (plan.md Complexity Tracking). An asyncio task inside
the API would cover a hang via timeout but not a hard crash or a C-extension deadlock, and it would
put ingest CPU in the request path against SC-007. Two processes make FR-025's containment a
property rather than a hope.

This module is the wiring: read config, build the engine, and run the loop until a signal arrives.
The interesting behaviour lives in :mod:`aggregato.sync.scheduler` and
:mod:`aggregato.sync.dispatch`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from aggregato.config import Config, ConfigError, load_config
from aggregato.db.engine import create_engine
from aggregato.db.migrate import upgrade_to_head
from aggregato.logging import configure_logging
from aggregato.sync.dispatch import build_dispatch
from aggregato.sync.scheduler import Scheduler

log = logging.getLogger(__name__)


async def serve(config: Config) -> None:
    """Run the scheduler until stopped.

    Args:
        config: Loaded configuration. The token is not needed here — the scheduler serves no HTTP —
            but ``load_config`` enforces it anyway, so a misconfigured install fails the same way in
            both processes rather than half-starting.
    """
    # The scheduler migrates too, rather than assuming the API went first. research.md R14 wants
    # this process independently restartable, and a scheduler that crash-loops on "no such table"
    # because it booted first is neither restartable nor diagnosable. upgrade_to_head is idempotent;
    # in a thread because Alembic's env.py runs its own asyncio.run.
    #
    # ponytail: two processes could migrate concurrently on a cold start. Alembic's version table
    # plus SQLite's busy_timeout serializes them in practice; a real advisory lock is the upgrade
    # path if a Postgres deployment ever shows a race.
    await asyncio.to_thread(upgrade_to_head, config.database_url)

    engine = create_engine(config.database_url)
    scheduler = Scheduler(engine, dispatch=build_dispatch(engine, config))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # A run in flight gets to finish its child process rather than being severed, which is what
        # keeps a restart from leaving a `syncing` row nobody will ever clear.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(scheduler.stop()))

    try:
        await scheduler.run_forever()
    finally:
        await engine.dispose()
        log.info("scheduler stopped")


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
