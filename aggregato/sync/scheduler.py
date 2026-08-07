"""The scheduler: a due-queue poll loop (research.md ).

There is no scheduling library and no cron expression. ``provider_state.next_run_at`` **is** the
schedule. The loop wakes every few seconds, selects providers whose time has come, and dispatches up
to a concurrency cap; every run's outcome rewrites ``next_run_at``.

Why that is the right shape rather than a smaller-looking one: what §7 actually requires is a
per-provider interval the *plugin* declares , jitter on boot, an escalating ladder that
overrides the interval after a failure , a ``degraded`` state that collapses the ladder back
, a rate-limit response that lengthens the interval for the session , and a full
record of every attempt with lineage . All of that is state that must survive a restart and
be visible in the UI, so it has to be in the database whatever triggers it. Once ``next_run_at`` is
persisted, the scheduler is a ``SELECT`` and a loop — and observability comes free, because the
schedule is a queryable table.

The loop never lets one provider affect another: each run is its own task around its own child
process, and a failure is recorded rather than raised .
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, or_, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import transaction
from aggregato.db.schema import provider_state, providers, sync_runs
from aggregato.domain.clock import SYSTEM_CLOCK, Clock
from aggregato.domain.enums import Capability, ErrorClass, ProviderStatus, RunStatus
from aggregato.logging import bind_run
from aggregato.providers.registry import discover_providers

#: How often the loop looks for due work. Seconds rather than minutes because a "sync now" button
#: writes ``next_run_at = now`` and the operator is watching; minutes rather than milliseconds
#: because run intervals are measured in hours.
POLL_INTERVAL_SECONDS = 5.0

#: How many runs may be in flight at once. Each is a process, so this is a memory and CPU bound, not
#: a politeness one — politeness is per host and lives in the HTTP client.
DEFAULT_MAX_CONCURRENT_RUNS = 3

# SQLite accepts only one writer at a time.  After downtime several providers can be due together;
# letting their ingest transactions overlap turns that backlog into avoidable ``database is locked``
# failures.  Other backends retain the normal parallelism.
SQLITE_MAX_CONCURRENT_RUNS = 1

#: Spread on the first scheduling after boot. Without it, providers enabled in one sitting sync in
#: lockstep forever after, turning a restart into a thundering herd against several platforms at
#: once .
BOOT_JITTER_SECONDS = 120

log = logging.getLogger(__name__)
_UNSET = object()


def max_concurrent_runs(database_url: str) -> int:
    """Return the safe scheduler concurrency for the configured database backend.

    SQLite's WAL mode lets reads proceed during a write, but it does not make writes concurrent.
    Serializing provider runs there is particularly important just after restart, when many overdue
    providers may be dispatched together.  Postgres and other supported server databases retain
    the regular worker parallelism.
    """
    if make_url(database_url).get_backend_name() == "sqlite":
        return SQLITE_MAX_CONCURRENT_RUNS
    return DEFAULT_MAX_CONCURRENT_RUNS


@dataclass(frozen=True)
class DueProvider:
    """A provider the loop has decided to run now."""

    provider_id: str
    interval_seconds: int
    retry_step: int
    consecutive_failures: int
    cursor: dict[str, object] | None
    requested_mode: str | None = None
    """What an operator asked for via ``POST /providers/{id}/sync``, if anything. ``None`` is the
    ordinary scheduled run, which is always incremental."""
    requested_lineage_id: UUID | None = None
    """The durable lineage assigned to an operator request, if one is pending."""


def _pollable_provider_ids(provider_dir: Path | None = None) -> frozenset[str]:
    """Return installed providers that declare the host-owned polling capability.

    Scheduler admission cannot import provider code to ask whether it can poll.  The registry's
    static manifests are the safe source of that decision; providers absent from the allowlist are
    deliberately not schedulable.
    """
    discovered = (
        discover_providers(provider_dir) if provider_dir is not None else discover_providers()
    )
    return frozenset(info.id for info in discovered if Capability.POLL.value in info.capabilities)


def _state_is_due(now: datetime) -> ColumnElement[bool]:
    """Build the provider-state predicate shared by due listing and the atomic claim."""
    return or_(provider_state.c.next_run_at.is_(None), provider_state.c.next_run_at <= now)


def _claim_due_state(now: datetime) -> ColumnElement[bool]:
    """Return an ``EXISTS`` predicate that rechecks the schedule during admission."""
    return exists(
        select(1)
        .select_from(provider_state)
        .where(
            provider_state.c.provider_id == providers.c.id,
            _state_is_due(now),
        )
    )


async def due_providers(
    engine: AsyncEngine, *, now: datetime, provider_dir: Path | None = None
) -> list[DueProvider]:
    """The providers whose ``next_run_at`` has arrived.

    This is the whole of the schedule (research.md ). ``next_run_at IS NULL`` counts as due, which
    is how a newly enabled provider gets its first run without a separate code path.

    Args:
        engine: The database engine.
        now: From the injected clock.

    Returns:
        Due providers, soonest first.
    """
    pollable_ids = _pollable_provider_ids(provider_dir)
    query = (
        select(
            provider_state.c.provider_id,
            provider_state.c.effective_interval_seconds,
            provider_state.c.retry_step,
            provider_state.c.consecutive_failures,
            provider_state.c.cursor,
            provider_state.c.requested_mode,
            provider_state.c.requested_lineage_id,
        )
        .join(providers, providers.c.id == provider_state.c.provider_id)
        .where(
            and_(
                providers.c.id.in_(pollable_ids),
                providers.c.enabled.is_(True),
                providers.c.status.in_((str(ProviderStatus.IDLE), str(ProviderStatus.DEGRADED))),
                _state_is_due(now),
            )
        )
        .order_by(provider_state.c.next_run_at.asc().nulls_first(), provider_state.c.provider_id)
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
                requested_mode=row.requested_mode,
                requested_lineage_id=row.requested_lineage_id,
            )
            for row in result
        ]


async def claim(
    engine: AsyncEngine,
    provider_id: str,
    *,
    now: datetime,
    provider_dir: Path | None = None,
) -> bool:
    """Mark a provider ``syncing``, and report whether this caller got it.

    The conditional ``UPDATE`` is the claim: two schedulers, or one scheduler with an overlapping
    poll, cannot both win. Every admission condition is in that same statement, so a stale due-list
    row cannot bypass a disable, a configuration failure, a capability change, or a newly-future
    schedule.
    """
    pollable_ids = _pollable_provider_ids(provider_dir)
    async with transaction(engine) as conn:
        result = await conn.execute(
            update(providers)
            .where(
                and_(
                    providers.c.id == provider_id,
                    providers.c.id.in_(pollable_ids),
                    providers.c.enabled.is_(True),
                    providers.c.status.in_(
                        (str(ProviderStatus.IDLE), str(ProviderStatus.DEGRADED))
                    ),
                    _claim_due_state(now),
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
            select(providers.c.id, providers.c.enabled).where(
                providers.c.status == str(ProviderStatus.SYNCING)
            )
        )
        provider_rows = list(result)
        provider_ids = [str(row.id) for row in provider_rows]
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
            .where(
                providers.c.id.in_(provider_ids),
                providers.c.status == str(ProviderStatus.SYNCING),
                providers.c.enabled.is_(True),
            )
            .values(status=str(ProviderStatus.IDLE), updated_at=now)
        )
        await conn.execute(
            update(providers)
            .where(
                providers.c.id.in_(provider_ids),
                providers.c.status == str(ProviderStatus.SYNCING),
                providers.c.enabled.is_(False),
            )
            .values(status=str(ProviderStatus.DISABLED), updated_at=now)
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
    run_id: int | None = None,
    run_status: RunStatus | None = None,
    items_seen: int | None = None,
    items_written: int | None = None,
    items_failed: int | None = None,
    error_class: ErrorClass | None = None,
    error_message: str | None = None,
    log_excerpt: str | None = None,
    cursor_after: dict[str, object] | None = None,
    requested_lineage_id: UUID | object | None = _UNSET,
) -> None:
    """Record a run's outcome and reschedule.

    Every field here comes from :mod:`aggregato.sync.retry`'s decision. The scheduler does not
    decide retry policy; it persists what the ladder said ( through ).
    """
    async with transaction(engine) as conn:
        if run_id is not None and run_status is not None:
            run_result = await conn.execute(
                update(sync_runs)
                .where(
                    sync_runs.c.id == run_id,
                    sync_runs.c.status == str(RunStatus.RUNNING),
                )
                .values(
                    status=str(run_status),
                    finished_at=now,
                    items_seen=items_seen or 0,
                    items_written=items_written or 0,
                    items_failed=items_failed or 0,
                    error_class=str(error_class) if error_class else None,
                    error_message=error_message,
                    log_excerpt=log_excerpt,
                    cursor_after=cursor_after,
                )
            )
            if run_result.rowcount != 1:
                raise RuntimeError(f"run {run_id} was not open when release finalized it")

        provider_values: dict[str, object] = {"status": str(status), "updated_at": now}
        if last_error is not None or status is ProviderStatus.IDLE:
            provider_values["last_error"] = last_error
        await conn.execute(
            update(providers)
            .where(
                providers.c.id == provider_id,
                providers.c.enabled.is_(True),
                providers.c.status == str(ProviderStatus.SYNCING),
            )
            .values(provider_values)
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
        if requested_lineage_id is not _UNSET:
            values["requested_lineage_id"] = requested_lineage_id
        if cursor_after is not None:
            values["cursor"] = cursor_after
        state_result = await conn.execute(
            update(provider_state).where(provider_state.c.provider_id == provider_id).values(values)
        )
        assert state_result.rowcount == 1, (
            f"release expected one provider_state row for {provider_id!r}, "
            f"updated {state_result.rowcount}"
        )


class Scheduler:
    """The poll loop.

    Time and randomness are injected (testing guidance): ``Clock`` so a test can advance to a due
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
        provider_dir: Path | None = None,
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
            provider_dir: Optional reviewed/unreviewed drop-in provider directory.
        """
        self._engine = engine
        self._dispatch = dispatch
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
        self._max_concurrent = max_concurrent
        self._provider_dir = provider_dir
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
        available_slots = self._max_concurrent - len(self._running)
        if available_slots <= 0:
            return started

        for due in await due_providers(
            self._engine, now=self._clock.now(), provider_dir=self._provider_dir
        ):
            if due.provider_id in self._running:
                continue
            # Claiming is itself a database write. Do not claim work that must wait for the
            # semaphore: on SQLite, that claim could collide with the active run's ingest
            # transaction even though the run tasks themselves are serialized.
            if available_slots == 0:
                break
            if not await claim(
                self._engine,
                due.provider_id,
                now=self._clock.now(),
                provider_dir=self._provider_dir,
            ):
                # Someone else got it. Not an error; the next poll will find it if it is still due.
                continue
            task = asyncio.create_task(self._guarded(due), name=f"sync:{due.provider_id}")
            self._running[due.provider_id] = task
            task.add_done_callback(self._forget(due.provider_id))
            started += 1
            available_slots -= 1
        return started

    def _forget(self, provider_id: str) -> Callable[[asyncio.Task[None]], None]:
        """Drop a finished run from the in-flight map, so the next poll may dispatch it again."""

        def done(_task: asyncio.Task[None]) -> None:
            self._running.pop(provider_id, None)

        return done

    async def _guarded(self, due: DueProvider) -> None:
        """Run one provider, holding the concurrency slot, swallowing nothing silently.

        An exception escaping here would kill the loop and with it every other provider, which is
        precisely what  forbids — so it is logged against the provider and the loop continues.
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
