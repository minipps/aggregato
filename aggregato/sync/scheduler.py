"""The scheduler: a due-queue poll loop (research.md R1).

There is no scheduling library and no cron expression. ``provider_state.next_run_at`` **is** the
schedule. The loop wakes every few seconds, selects providers whose time has come, and dispatches up
to a concurrency cap; every run's outcome rewrites ``next_run_at``.

Why that is the right shape rather than a smaller-looking one: what §7 actually requires is a
per-provider interval the *plugin* declares (FR-018), jitter on boot, an escalating ladder that
overrides the interval after a failure (FR-020), a ``degraded`` state that collapses the ladder back
(FR-021), a rate-limit response that lengthens the interval for the session (FR-022), and a full
record of every attempt with lineage (FR-019). All of that is state that must survive a restart and
be visible in the UI, so it has to be in the database whatever triggers it. Once ``next_run_at`` is
persisted, the scheduler is a ``SELECT`` and a loop — and observability comes free, because the
schedule is a queryable table.

The loop never lets one provider affect another: each run is its own task around its own child
process, and a failure is recorded rather than raised (FR-025).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import transaction
from aggregato.db.schema import provider_state, providers, sync_runs
from aggregato.domain.clock import SYSTEM_CLOCK, Clock
from aggregato.domain.enums import ErrorClass, ProviderStatus, RunStatus
from aggregato.logging import bind_run

#: How often the loop looks for due work. Seconds rather than minutes because a "sync now" button
#: writes ``next_run_at = now`` and the operator is watching; minutes rather than milliseconds
#: because run intervals are measured in hours.
POLL_INTERVAL_SECONDS = 5.0

#: How many runs may be in flight at once. Each is a process, so this is a memory and CPU bound, not
#: a politeness one — politeness is per host and lives in the HTTP client.
DEFAULT_MAX_CONCURRENT_RUNS = 3

#: Spread on the first scheduling after boot. Without it, providers enabled in one sitting sync in
#: lockstep forever after, turning a restart into a thundering herd against several platforms at
#: once (FR-018).
BOOT_JITTER_SECONDS = 120

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DueProvider:
    """A provider the loop has decided to run now."""

    provider_id: str
    interval_seconds: int
    retry_step: int
    consecutive_failures: int
    cursor: dict[str, object] | None


async def due_providers(engine: AsyncEngine, *, now: datetime) -> list[DueProvider]:
    """The providers whose ``next_run_at`` has arrived.

    This is the whole of the schedule (research.md R1). ``next_run_at IS NULL`` counts as due, which
    is how a newly enabled provider gets its first run without a separate code path.

    Args:
        engine: The database engine.
        now: From the injected clock.

    Returns:
        Due providers, soonest first.
    """
    query = (
        select(
            provider_state.c.provider_id,
            provider_state.c.effective_interval_seconds,
            provider_state.c.retry_step,
            provider_state.c.consecutive_failures,
            provider_state.c.cursor,
        )
        .join(providers, providers.c.id == provider_state.c.provider_id)
        .where(
            and_(
                providers.c.enabled.is_(True),
                providers.c.status != str(ProviderStatus.DISABLED),
                # A run already in flight must not be dispatched twice. The status is the lock.
                providers.c.status != str(ProviderStatus.SYNCING),
                provider_state.c.next_run_at.is_(None) | (provider_state.c.next_run_at <= now),
            )
        )
        .order_by(provider_state.c.next_run_at.asc().nulls_first())
    )
    async with transaction(engine) as conn:
        result = await conn.execute(query)
        return [
            DueProvider(
                provider_id=row.provider_id,
                interval_seconds=row.effective_interval_seconds,
                retry_step=row.retry_step,
                consecutive_failures=row.consecutive_failures,
                cursor=row.cursor,
            )
            for row in result
        ]


async def claim(engine: AsyncEngine, provider_id: str, *, now: datetime) -> bool:
    """Mark a provider ``syncing``, and report whether this caller got it.

    The ``status != 'syncing'`` predicate in the ``UPDATE`` is the claim: two schedulers, or one
    scheduler with an overlapping poll, cannot both win. Doing it in SQL rather than with an
    in-memory set is what makes it survive a restart mid-run.
    """
    async with transaction(engine) as conn:
        result = await conn.execute(
            update(providers)
            .where(
                and_(
                    providers.c.id == provider_id,
                    providers.c.status != str(ProviderStatus.SYNCING),
                )
            )
            .values(status=str(ProviderStatus.SYNCING), updated_at=now)
        )
        return result.rowcount == 1


async def recover_interrupted_runs(engine: AsyncEngine, *, now: datetime) -> list[str]:
    """Release provider locks and close run records left by a stopped worker.

    A provider's ``syncing`` status is a durable lock.  It is normally released by the dispatch
    path, but a container can be killed between recording a run and its cleanup.  Without this
    recovery, that durable lock prevents every later scheduled and manual sync forever.  The cursor
    is intentionally untouched: re-running a partially completed page is safe because ingest is
    idempotent.
    """
    async with transaction(engine) as conn:
        result = await conn.execute(
            select(providers.c.id).where(providers.c.status == str(ProviderStatus.SYNCING))
        )
        provider_ids = [str(row.id) for row in result]
        if not provider_ids:
            return []

        await conn.execute(
            update(sync_runs)
            .where(
                and_(
                    sync_runs.c.provider_id.in_(provider_ids),
                    sync_runs.c.status == str(RunStatus.RUNNING),
                )
            )
            .values(
                status=str(RunStatus.FAILED),
                finished_at=now,
                error_class=str(ErrorClass.INTERNAL),
                error_message="worker stopped before this sync completed; it will be retried",
            )
        )
        await conn.execute(
            update(providers)
            .where(providers.c.id.in_(provider_ids))
            .values(status=str(ProviderStatus.IDLE), updated_at=now)
        )
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id.in_(provider_ids))
            .values(next_run_at=now)
        )
    return provider_ids


async def release(
    engine: AsyncEngine,
    provider_id: str,
    *,
    status: ProviderStatus,
    next_run_at: datetime | None,
    retry_step: int,
    consecutive_failures: int,
    now: datetime,
    last_success_at: datetime | None = None,
    last_error: dict[str, str] | None = None,
    effective_interval_seconds: int | None = None,
) -> None:
    """Record a run's outcome and reschedule.

    Every field here comes from :mod:`aggregato.sync.retry`'s decision. The scheduler does not
    decide retry policy; it persists what the ladder said (FR-019 through FR-022).
    """
    async with transaction(engine) as conn:
        provider_values: dict[str, object] = {"status": str(status), "updated_at": now}
        if last_error is not None or status is ProviderStatus.IDLE:
            provider_values["last_error"] = last_error
        await conn.execute(
            update(providers).where(providers.c.id == provider_id).values(provider_values)
        )
        values: dict[str, object] = {
            "next_run_at": next_run_at,
            "retry_step": retry_step,
            "consecutive_failures": consecutive_failures,
        }
        if last_success_at is not None:
            values["last_success_at"] = last_success_at
        if effective_interval_seconds is not None:
            values["effective_interval_seconds"] = effective_interval_seconds
        await conn.execute(
            update(provider_state).where(provider_state.c.provider_id == provider_id).values(values)
        )


class Scheduler:
    """The poll loop.

    Time and randomness are injected (Constitution II): ``Clock`` so a test can advance to a due
    moment without waiting, ``Random`` so jitter is reproducible. Neither is read ambiently.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        dispatch: Callable[[DueProvider], Awaitable[None]],
        clock: Clock = SYSTEM_CLOCK,
        rng: random.Random | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_RUNS,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        """
        Args:
            engine: The database engine.
            dispatch: An awaitable called with one :class:`DueProvider`. Injected rather than
                imported so the loop can be tested without spawning processes, and so the API's
                "sync now" path and the loop share one execution route.
            clock: Time source.
            rng: Jitter source.
            max_concurrent: In-flight run cap.
            poll_interval_seconds: How long to sleep between polls.
        """
        self._engine = engine
        self._dispatch = dispatch
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._poll_interval = poll_interval_seconds
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stopped = asyncio.Event()

    def boot_jitter(self) -> timedelta:
        """A spread for the first scheduling after boot, so providers do not sync in lockstep."""
        return timedelta(seconds=self._rng.uniform(0, BOOT_JITTER_SECONDS))

    async def poll_once(self) -> int:
        """Dispatch every currently-due provider. Returns how many runs were started.

        Separated from :meth:`run_forever` so tests drive one iteration at a time against a frozen
        clock rather than racing a sleep.
        """
        started = 0
        for due in await due_providers(self._engine, now=self._clock.now()):
            if due.provider_id in self._running:
                continue
            if not await claim(self._engine, due.provider_id, now=self._clock.now()):
                # Someone else got it. Not an error; the next poll will find it if it is still due.
                continue
            task = asyncio.create_task(self._guarded(due), name=f"sync:{due.provider_id}")
            self._running[due.provider_id] = task
            task.add_done_callback(self._forget(due.provider_id))
            started += 1
        return started

    def _forget(self, provider_id: str) -> Callable[[asyncio.Task[None]], None]:
        """Drop a finished run from the in-flight map, so the next poll may dispatch it again."""

        def done(_task: asyncio.Task[None]) -> None:
            self._running.pop(provider_id, None)

        return done

    async def _guarded(self, due: DueProvider) -> None:
        """Run one provider, holding the concurrency slot, swallowing nothing silently.

        An exception escaping here would kill the loop and with it every other provider, which is
        precisely what FR-025 forbids — so it is logged against the provider and the loop continues.
        """
        async with self._semaphore:
            # bind_run is a sync context manager: contextvars need no await, and the binding
            # follows the task rather than the connection.
            with bind_run(provider_id=due.provider_id):
                try:
                    await self._dispatch(due)
                except Exception:
                    log.exception(
                        "sync run for %s failed outside the child process", due.provider_id
                    )
                    await release(
                        self._engine,
                        due.provider_id,
                        status=ProviderStatus.DEGRADED,
                        next_run_at=self._clock.now() + timedelta(seconds=due.interval_seconds),
                        retry_step=due.retry_step,
                        consecutive_failures=due.consecutive_failures + 1,
                        now=self._clock.now(),
                    )

    async def run_forever(self) -> None:
        """Poll until stopped. The scheduler process's main loop."""
        log.info("scheduler started, polling every %.0fs", self._poll_interval)
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except Exception:
                # A database hiccup must not end the loop; the next poll retries.
                log.exception("scheduler poll failed")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._poll_interval):
                    await self._stopped.wait()

    async def stop(self) -> None:
        """Ask the loop to finish and wait for in-flight runs."""
        self._stopped.set()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
