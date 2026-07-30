"""One sync run, end to end: spawn, ingest, classify, reschedule.

This is the seam where the four pieces built separately meet — :mod:`runner` supervises the child,
:mod:`aggregato.ingest.writer` writes what it produced, :mod:`errors` classifies a failure, and
:mod:`retry` decides when to try again. Keeping it separate means the scheduler holds a poll loop
and nothing else, and the API's "sync now" path calls the same route rather than a parallel one that
drifts.

The order matters and is not arbitrary:

1. Write the ``sync_runs`` row **first**, because ``ingest_failures`` references it and because a
   run that vanishes without a trace is the failure mode operators cannot diagnose.
2. Run the child, and write what it produced even if it ended badly — records that arrived and
   validated are real, and discarding them would make a mid-run failure lose data (FR-020).
3. Advance the cursor only to the last checkpoint the child actually flushed. Never further.
4. Reschedule from the ladder's decision, never from a number computed here.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config
from aggregato.db.engine import transaction
from aggregato.db.schema import import_jobs, provider_state, sync_runs
from aggregato.domain.clock import SYSTEM_CLOCK, Clock
from aggregato.domain.enums import (
    Capability,
    ErrorClass,
    FetchMode,
    IngestStage,
    ProviderStatus,
    RunStatus,
)
from aggregato.domain.models import Cursor
from aggregato.ingest.failures import capture_failure
from aggregato.ingest.writer import WriteContext, ensure_rating_scales, infer_deletes, write_batches
from aggregato.providers.registry import load_provider
from aggregato.sync.errors import action_required, schedules_retry
from aggregato.sync.retry import plan_after_failure, plan_after_success
from aggregato.sync.runner import RunOutcome, RunRequest, execute_run
from aggregato.sync.sanity import assess_window
from aggregato.sync.scheduler import DueProvider, release

log = logging.getLogger(__name__)


def build_dispatch(
    engine: AsyncEngine,
    config: Config,
    *,
    clock: Clock = SYSTEM_CLOCK,
) -> Callable[[DueProvider], Awaitable[None]]:
    """Build the callable the scheduler invokes for one due provider.

    A closure rather than a class: it captures three things and has one method, and a class here
    would be an object with no state of its own.
    """

    async def dispatch(due: DueProvider) -> None:
        job = await _next_import_job(engine, due.provider_id)
        await run_once(
            engine,
            config,
            provider_id=due.provider_id,
            mode=FetchMode.IMPORT if job is not None else FetchMode.INCREMENTAL,
            cursor=None if job is not None else (Cursor(state=due.cursor) if due.cursor else None),
            retry_step=due.retry_step,
            consecutive_failures=due.consecutive_failures,
            interval_seconds=due.interval_seconds,
            clock=clock,
            import_path=Path(job.path) if job is not None else None,
        )
        if job is not None:
            await _finish_import_job(engine, job.id)

    return dispatch


async def _next_import_job(engine: AsyncEngine, provider_id: str) -> object | None:
    """Claim the oldest queued upload for a provider already claimed by the scheduler."""
    async with transaction(engine) as conn:
        job = (
            await conn.execute(
                select(import_jobs)
                .where(import_jobs.c.provider_id == provider_id, import_jobs.c.started_at.is_(None))
                .order_by(import_jobs.c.id)
                .limit(1)
            )
        ).first()
        if job is not None:
            await conn.execute(
                update(import_jobs)
                .where(import_jobs.c.id == job.id)
                .values(started_at=SYSTEM_CLOCK.now())
            )
        return job


async def _finish_import_job(engine: AsyncEngine, job_id: int) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            update(import_jobs)
            .where(import_jobs.c.id == job_id)
            .values(finished_at=SYSTEM_CLOCK.now())
        )


async def run_once(
    engine: AsyncEngine,
    config: Config,
    *,
    provider_id: str,
    mode: FetchMode,
    cursor: Cursor | None,
    retry_step: int,
    consecutive_failures: int,
    interval_seconds: int,
    clock: Clock = SYSTEM_CLOCK,
    lineage_id: uuid.UUID | None = None,
    import_path: Path | None = None,
) -> RunOutcome:
    """Execute and record one sync run.

    Args:
        engine: The database engine. The child never sees it (FR-037).
        config: For the provider's configuration and secrets.
        provider_id: Which provider to run.
        mode: ``incremental``, ``full``, or ``import``.
        cursor: Where to resume.
        retry_step: The provider's current ladder position.
        consecutive_failures: Its current failure streak.
        interval_seconds: Its normal interval.
        clock: Time source.
        lineage_id: Set when this run is a retry of earlier work, so the UI groups attempts
            (FR-019). ``None`` starts a new lineage.
        import_path: Set only in ``import`` mode.

    Returns:
        The run's outcome, already persisted.
    """
    now = clock.now()
    provider = load_provider(provider_id)
    provider_config = config.providers.get(provider_id)
    lineage = lineage_id or uuid.uuid4()
    attempt = retry_step + 1

    run_id = await _open_run(
        engine,
        provider_id=provider_id,
        lineage_id=lineage,
        attempt=attempt,
        mode=mode,
        cursor_before=cursor,
        now=now,
    )

    outcome = await execute_run(
        RunRequest(
            provider_id=provider_id,
            mode=mode,
            cursor=cursor,
            config=dict(provider_config.settings) if provider_config else {},
            # `secrets` stays empty: config.py resolves ${VAR} references into `settings` at read
            # time (research.md R15), so a provider's credentials already arrive inside its own
            # validated config block. A second channel would be two places to leak from.
            secrets={},
            import_path=import_path,
        )
    )

    written = await _ingest(
        engine, provider, outcome, provider_id=provider_id, run_id=run_id, now=now
    )
    sanity_passed = await _apply_full_run_guards(
        engine,
        provider=provider,
        provider_id=provider_id,
        mode=mode,
        outcome=outcome,
        item_count=len(outcome.records) + len(outcome.failures),
        run_started_at=now,
        config=provider_config.settings if provider_config else {},
    )
    await _close_run(engine, run_id, outcome=outcome, written=written, now=clock.now())
    await _reschedule(
        engine,
        provider_id=provider_id,
        outcome=outcome,
        retry_step=retry_step,
        consecutive_failures=consecutive_failures,
        interval_seconds=interval_seconds,
        lineage_id=lineage,
        clock=clock,
        sanity_passed=sanity_passed,
    )
    return outcome


async def _open_run(
    engine: AsyncEngine,
    *,
    provider_id: str,
    lineage_id: uuid.UUID,
    attempt: int,
    mode: FetchMode,
    cursor_before: Cursor | None,
    now: datetime,
) -> int:
    """Record the run as ``running`` before anything can go wrong."""
    async with transaction(engine) as conn:
        result = await conn.execute(
            sync_runs.insert().values(
                provider_id=provider_id,
                lineage_id=lineage_id,
                attempt=attempt,
                mode=str(mode),
                status=str(RunStatus.RUNNING),
                started_at=now,
                cursor_before=cursor_before.state if cursor_before else None,
            )
        )
        primary_key = result.inserted_primary_key
        assert primary_key is not None
        return int(primary_key[0])


async def _ingest(
    engine: AsyncEngine,
    provider: object,
    outcome: RunOutcome,
    *,
    provider_id: str,
    run_id: int,
    now: datetime,
) -> int:
    """Write what the child produced, in one transaction.

    Called even when the run failed: records that arrived and validated are real, and throwing them
    away would turn a mid-run failure into data loss rather than a partial success (FR-020).
    """
    scales = list(getattr(provider, "rating_scales", []))
    async with transaction(engine) as conn:
        await ensure_rating_scales(conn, scales)
        counts = await write_batches(
            conn,
            WriteContext(
                provider_id=provider_id,
                sync_run_id=run_id,
                schema_version=int(getattr(provider, "schema_version", 1)),
                now=now,
                rating_scales={scale.id: scale for scale in scales},
            ),
            outcome.records,
        )
        # Records the CHILD could not normalize, stored with their payloads so a fixed provider can
        # replay them (FR-023). Distinct from records the writer rejected, which write_batches
        # already captured.
        for failure in outcome.failures:
            await capture_failure(
                conn,
                provider_id=provider_id,
                sync_run_id=run_id,
                stage=IngestStage.NORMALIZE,
                error=failure.error,
                raw_payload=dict(failure.payload),
                now=now,
            )
    return counts.written


async def _close_run(
    engine: AsyncEngine,
    run_id: int,
    *,
    outcome: RunOutcome,
    written: int,
    now: datetime,
) -> None:
    """Finalize the ``sync_runs`` row with counts and any classification."""
    async with transaction(engine) as conn:
        await conn.execute(
            update(sync_runs)
            .where(sync_runs.c.id == run_id)
            .values(
                status=str(outcome.status),
                finished_at=now,
                items_seen=len(outcome.records) + len(outcome.failures),
                items_written=written,
                items_failed=len(outcome.failures),
                error_class=str(outcome.error_class) if outcome.error_class else None,
                error_message=outcome.error_message,
                log_excerpt=outcome.log_excerpt,
                cursor_after=outcome.cursor_after.state if outcome.cursor_after else None,
            )
        )


async def _reschedule(
    engine: AsyncEngine,
    *,
    provider_id: str,
    outcome: RunOutcome,
    retry_step: int,
    consecutive_failures: int,
    interval_seconds: int,
    lineage_id: uuid.UUID,
    clock: Clock,
    sanity_passed: bool = True,
) -> None:
    """Advance the cursor and set the next run time from the ladder's decision."""
    from datetime import timedelta

    normal = timedelta(seconds=interval_seconds)

    if outcome.status is RunStatus.SUCCESS:
        decision = plan_after_success(clock=clock, normal_interval=normal)
        status = ProviderStatus.IDLE
        last_success = clock.now()
    else:
        decision = plan_after_failure(
            clock=clock,
            error_class=outcome.error_class or ErrorClass.INTERNAL,
            retry_step=retry_step,
            consecutive_failures=consecutive_failures,
            normal_interval=normal,
            lineage_id=lineage_id,
            retry_after=outcome.retry_after,
        )
        # auth, blocked, and structure_changed never schedule a retry, and the provider goes
        # degraded immediately — waiting does not fix any of them (contract §4).
        status = (
            ProviderStatus.DEGRADED
            if not schedules_retry(outcome.error_class or ErrorClass.INTERNAL)
            or decision.status is ProviderStatus.DEGRADED
            else ProviderStatus.IDLE
        )
        last_success = None

    await release(
        engine,
        provider_id,
        status=status,
        next_run_at=decision.next_run_at,
        retry_step=decision.retry_step,
        consecutive_failures=decision.consecutive_failures,
        now=clock.now(),
        last_success_at=last_success,
        last_error=(
            None
            if outcome.status is RunStatus.SUCCESS
            else {
                "error_class": str(outcome.error_class or ErrorClass.INTERNAL),
                "message": outcome.error_message or "sync did not complete",
                "action_required": action_required(outcome.error_class or ErrorClass.INTERNAL),
            }
        ),
        effective_interval_seconds=(
            max(interval_seconds, int(outcome.retry_after.total_seconds()))
            if outcome.error_class is ErrorClass.RATE_LIMIT and outcome.retry_after is not None
            else None
        ),
    )

    # The cursor advances ONLY to the last checkpoint the child actually flushed — never to where
    # the child claimed it reached, and never at all on a run with no checkpoint (FR-020).
    if outcome.cursor_after is not None:
        async with transaction(engine) as conn:
            await conn.execute(
                update(provider_state)
                .where(provider_state.c.provider_id == provider_id)
                .values(cursor=outcome.cursor_after.state)
            )


async def _apply_full_run_guards(
    engine: AsyncEngine,
    *,
    provider: object,
    provider_id: str,
    mode: FetchMode,
    outcome: RunOutcome,
    item_count: int,
    run_started_at: datetime,
    config: dict[str, object],
) -> bool:
    """Record the full-window baseline and permit tombstones only on a sane, opted-in full run."""
    if mode is not FetchMode.FULL or outcome.status is not RunStatus.SUCCESS:
        return False
    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(provider_state.c.last_window_item_count).where(
                    provider_state.c.provider_id == provider_id
                )
            )
        ).first()
        result = assess_window(item_count, row.last_window_item_count if row else None)
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == provider_id)
            .values(last_window_item_count=item_count)
        )
        if not result.passed:
            outcome.status = RunStatus.PARTIAL
            outcome.error_class = ErrorClass.PARSE
            outcome.error_message = (
                f"full fetch returned {item_count} items, below the sanity threshold "
                f"of {result.minimum_count} from the prior window"
            )
            return False
        capabilities = set(getattr(provider, "capabilities", set()))
        if (
            bool(config.get("infer_deletes", False))
            and Capability.REPORTS_DELETES not in capabilities
        ):
            await infer_deletes(conn, provider_id=provider_id, seen_since=run_started_at)
        return True


async def current_cursor(engine: AsyncEngine, provider_id: str) -> Cursor | None:
    """The stored cursor for a provider, for a manually triggered run."""
    async with transaction(engine) as conn:
        result = await conn.execute(
            select(provider_state.c.cursor).where(provider_state.c.provider_id == provider_id)
        )
        row = result.first()
    if row is None or row.cursor is None:
        return None
    return Cursor(state=row.cursor)
