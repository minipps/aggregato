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
   validated are real, and discarding them would make a mid-run failure lose data .
3. Advance the cursor only to the last checkpoint the child actually flushed. Never further.
4. Reschedule from the ladder's decision, never from a number computed here.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config
from aggregato.db.engine import transaction
from aggregato.db.schema import (
    import_jobs,
    ingest_failures,
    provider_state,
    providers,
    replay_jobs,
    sync_runs,
)
from aggregato.domain.clock import SYSTEM_CLOCK, Clock
from aggregato.domain.enums import (
    Capability,
    ErrorClass,
    FetchMode,
    IngestStage,
    ProviderStatus,
    RunStatus,
)
from aggregato.domain.models import Cursor, RawRecord
from aggregato.ingest.failures import capture_failure
from aggregato.ingest.normalize_replay import records_needing_replay, tombstone_replay_derivatives
from aggregato.ingest.resolve_queue import supersede_stale_open_items
from aggregato.ingest.writer import WriteContext, ensure_rating_scales, infer_deletes, write_batches
from aggregato.providers.registry import ProviderInfo, discover_providers
from aggregato.sync.errors import action_required, schedules_retry
from aggregato.sync.retry import plan_after_failure, plan_after_success
from aggregato.sync.runner import RunOutcome, RunRequest, execute_run
from aggregato.sync.sanity import assess_window
from aggregato.sync.scheduler import DueProvider, release

log = logging.getLogger(__name__)

IMPORT_LEASE = timedelta(minutes=15)
REPLAY_LEASE = timedelta(minutes=15)


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
        now = clock.now()
        job = await _next_import_job(engine, due.provider_id, now=now)
        replay_job = (
            None if job is not None else await _next_replay_job(engine, due.provider_id, now=now)
        )
        # Import/replay jobs have their own durable lineage and must not consume an ordinary sync
        # request while they are being serviced. For an ordinary run, request consumption happens
        # inside the same transaction that inserts sync_runs below.
        ordinary_request = job is None and replay_job is None
        requested_mode = due.requested_mode if ordinary_request else None
        requested_lineage = due.requested_lineage_id if due.requested_mode is not None else None
        preserve_requested_request = not ordinary_request and due.requested_mode is not None
        requested = (
            FetchMode.FULL if requested_mode == str(FetchMode.FULL) else FetchMode.INCREMENTAL
        )
        lineage = (
            job[2]
            if job is not None and job[2] is not None
            else replay_job[1]
            if replay_job is not None
            else requested_lineage
        )
        try:
            outcome = await run_once(
                engine,
                config,
                provider_id=due.provider_id,
                mode=(
                    FetchMode.IMPORT
                    if job is not None
                    else FetchMode.REPLAY
                    if replay_job is not None
                    else FetchMode.CHECK
                    if requested_mode == str(FetchMode.CHECK)
                    else requested
                ),
                cursor=(
                    None
                    if (
                        job is not None
                        or replay_job is not None
                        or requested_mode == str(FetchMode.CHECK)
                    )
                    else (Cursor(state=due.cursor) if due.cursor else None)
                ),
                retry_step=due.retry_step,
                consecutive_failures=due.consecutive_failures,
                interval_seconds=due.interval_seconds,
                clock=clock,
                lineage_id=lineage,
                requested_mode=requested_mode,
                requested_lineage_id=requested_lineage,
                preserve_requested_request=preserve_requested_request,
                import_path=job[1] if job is not None else None,
                replay_records=[replay_job[2]] if replay_job is not None else None,
                replay_only=replay_job is not None,
                check_restore_status=due.status if requested_mode == str(FetchMode.CHECK) else None,
                check_restore_enabled=due.enabled
                if requested_mode == str(FetchMode.CHECK)
                else None,
            )
        except Exception as exc:
            if job is not None:
                await _fail_import_job(engine, job[0], owner=job[3], error=exc, now=clock.now())
            if replay_job is not None:
                await _fail_replay_job(
                    engine, replay_job[0], owner=replay_job[3], error=exc, now=clock.now()
                )
            raise
        if job is not None:
            if outcome.status is RunStatus.SUCCESS:
                finished = await _finish_import_job(engine, job[0], owner=job[3], now=clock.now())
                if finished:
                    await _remove_import_file(job[1])
            else:
                await _fail_import_job(
                    engine,
                    job[0],
                    owner=job[3],
                    error=RuntimeError(outcome.error_message or "import did not complete"),
                    now=clock.now(),
                )
        if replay_job is not None:
            if outcome.status is RunStatus.SUCCESS and not outcome.failures:
                await _finish_replay_job(
                    engine, replay_job[0], replay_job[4], owner=replay_job[3], now=clock.now()
                )
            else:
                await _fail_replay_job(
                    engine,
                    replay_job[0],
                    owner=replay_job[3],
                    error=RuntimeError(outcome.error_message or "replay did not complete"),
                    now=clock.now(),
                )

    return dispatch


async def _next_import_job(
    engine: AsyncEngine, provider_id: str, *, now: datetime
) -> tuple[int, Path, uuid.UUID | None, str] | None:
    """Claim the oldest queued upload with a lease, recovering expired workers."""
    owner = uuid.uuid4().hex
    async with transaction(engine) as conn:
        job = (
            await conn.execute(
                select(import_jobs)
                .where(
                    import_jobs.c.provider_id == provider_id,
                    import_jobs.c.finished_at.is_(None),
                    import_jobs.c.failed_at.is_(None),
                    or_(
                        import_jobs.c.started_at.is_(None),
                        import_jobs.c.lease_expires_at.is_(None),
                        import_jobs.c.lease_expires_at <= now,
                    ),
                )
                .order_by(import_jobs.c.id)
                .limit(1)
            )
        ).first()
        if job is not None:
            job_id = int(job._mapping["id"])
            path = Path(str(job._mapping["path"]))
            result = await conn.execute(
                update(import_jobs)
                .where(
                    import_jobs.c.id == job_id,
                    import_jobs.c.finished_at.is_(None),
                    import_jobs.c.failed_at.is_(None),
                    or_(
                        import_jobs.c.started_at.is_(None),
                        import_jobs.c.lease_expires_at.is_(None),
                        import_jobs.c.lease_expires_at <= now,
                    ),
                )
                .values(
                    started_at=now,
                    lease_owner=owner,
                    lease_expires_at=now + IMPORT_LEASE,
                    attempts=func.coalesce(import_jobs.c.attempts, 0) + 1,
                )
            )
            if result.rowcount != 1:
                return None
            return job_id, path, job._mapping.get("lineage_id"), owner
        return None


async def _finish_import_job(
    engine: AsyncEngine, job_id: int, *, owner: str, now: datetime
) -> bool:
    async with transaction(engine) as conn:
        result = await conn.execute(
            update(import_jobs)
            .where(
                import_jobs.c.id == job_id,
                import_jobs.c.finished_at.is_(None),
                import_jobs.c.lease_owner == owner,
            )
            .values(finished_at=now, lease_owner=None, lease_expires_at=None)
        )
        return result.rowcount == 1


async def _fail_import_job(
    engine: AsyncEngine, job_id: int, *, owner: str, error: BaseException, now: datetime
) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            update(import_jobs)
            .where(import_jobs.c.id == job_id, import_jobs.c.lease_owner == owner)
            .values(
                failed_at=now,
                error_class=type(error).__name__,
                error=str(error)[:1000],
                lease_owner=None,
                lease_expires_at=None,
            )
        )


async def _next_replay_job(
    engine: AsyncEngine, provider_id: str, *, now: datetime
) -> tuple[int, uuid.UUID, RawRecord, str, int] | None:
    """Claim one captured failure for isolated child replay, with an expiring lease."""
    owner = uuid.uuid4().hex
    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(
                    replay_jobs.c.id,
                    replay_jobs.c.failure_id,
                    replay_jobs.c.lineage_id,
                    ingest_failures.c.native_id,
                    ingest_failures.c.raw_payload,
                )
                .join(ingest_failures, ingest_failures.c.id == replay_jobs.c.failure_id)
                .where(
                    replay_jobs.c.provider_id == provider_id,
                    replay_jobs.c.finished_at.is_(None),
                    replay_jobs.c.failed_at.is_(None),
                    or_(
                        replay_jobs.c.claimed_at.is_(None),
                        replay_jobs.c.lease_expires_at.is_(None),
                        replay_jobs.c.lease_expires_at <= now,
                    ),
                    ingest_failures.c.resolved_at.is_(None),
                    ingest_failures.c.native_id.is_not(None),
                )
                .order_by(replay_jobs.c.id)
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        job_id = int(row.id)
        result = await conn.execute(
            update(replay_jobs)
            .where(
                replay_jobs.c.id == job_id,
                replay_jobs.c.finished_at.is_(None),
                replay_jobs.c.failed_at.is_(None),
                or_(
                    replay_jobs.c.claimed_at.is_(None),
                    replay_jobs.c.lease_expires_at.is_(None),
                    replay_jobs.c.lease_expires_at <= now,
                ),
            )
            .values(
                claimed_at=now,
                lease_owner=owner,
                lease_expires_at=now + REPLAY_LEASE,
            )
        )
        if result.rowcount != 1:
            return None
        assert row.lineage_id is not None and row.native_id is not None
        return (
            job_id,
            row.lineage_id,
            RawRecord(native_id=str(row.native_id), payload=dict(row.raw_payload)),
            owner,
            int(row.failure_id),
        )


async def _finish_replay_job(
    engine: AsyncEngine, job_id: int, failure_id: int, *, owner: str, now: datetime
) -> None:
    async with transaction(engine) as conn:
        result = await conn.execute(
            update(replay_jobs)
            .where(replay_jobs.c.id == job_id, replay_jobs.c.lease_owner == owner)
            .values(finished_at=now, lease_owner=None, lease_expires_at=None)
        )
        if result.rowcount == 1:
            await conn.execute(
                update(ingest_failures)
                .where(
                    ingest_failures.c.id == failure_id,
                    ingest_failures.c.resolved_at.is_(None),
                )
                .values(resolved_at=now)
            )


async def _fail_replay_job(
    engine: AsyncEngine, job_id: int, *, owner: str, error: BaseException, now: datetime
) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            update(replay_jobs)
            .where(replay_jobs.c.id == job_id, replay_jobs.c.lease_owner == owner)
            .values(
                failed_at=now,
                error=type(error).__name__ + ": " + str(error)[:1000],
                lease_owner=None,
                lease_expires_at=None,
            )
        )


async def _remove_import_file(path: Path) -> None:
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except OSError:
        log.warning("could not remove completed import file %s", path, exc_info=True)


async def _provider_settings(
    engine: AsyncEngine, config: Config, provider_id: str
) -> dict[str, object]:
    """Read an operator's saved web settings, falling back to startup configuration.

    The database row is the mutable configuration layer. It is read immediately before spawning
    the child so a web edit takes effect on the next scheduled run without restarting the worker.
    Empty rows are the legacy/never-configured state and retain the YAML/environment settings.
    """
    async with transaction(engine) as conn:
        stored = (
            await conn.execute(select(providers.c.config).where(providers.c.id == provider_id))
        ).scalar_one_or_none()
    if isinstance(stored, dict) and stored:
        return dict(stored)
    fallback = config.providers.get(provider_id)
    return dict(fallback.settings) if fallback is not None else {}


def _provider_info(provider_id: str, provider_dir: Path | None) -> ProviderInfo:
    for info in discover_providers(provider_dir):
        if info.id == provider_id:
            return info
    raise LookupError(f"no provider with id {provider_id!r} is installed")


def _public_provider_settings(
    schema: dict[str, object], settings: dict[str, object]
) -> dict[str, object]:
    public, _ = _split_provider_settings(schema, settings)
    return public


def _secret_provider_settings(
    schema: dict[str, object], settings: dict[str, object]
) -> dict[str, str]:
    _, secrets = _split_provider_settings(schema, settings)
    return secrets


def _split_provider_settings(
    schema: dict[str, object], settings: dict[str, object], prefix: str = ""
) -> tuple[dict[str, object], dict[str, str]]:
    properties = _schema_properties(schema)
    public: dict[str, object] = {}
    secrets: dict[str, str] = {}
    for key, value in settings.items():
        node = properties.get(key, {})
        path = f"{prefix}.{key}" if prefix else key
        if _schema_secret(key, node):
            secrets[path] = str(value)
        elif isinstance(value, dict) and isinstance(node, dict):
            nested_public, nested_secrets = _split_provider_settings(node, value, path)
            public[key] = nested_public
            secrets.update(nested_secrets)
        else:
            public[key] = value
    return public, secrets


def _schema_properties(schema: dict[str, object]) -> dict[str, dict[str, object]]:
    properties: dict[str, dict[str, object]] = {}
    for candidate in _schema_variants(schema):
        value = candidate.get("properties")
        if isinstance(value, dict):
            properties.update(
                {key: child for key, child in value.items() if isinstance(child, dict)}
            )
    return properties


def _schema_variants(schema: dict[str, object]) -> list[dict[str, object]]:
    variants = [schema]
    for key in ("anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        if isinstance(value, list):
            variants.extend(child for child in value if isinstance(child, dict))
    return variants


def _schema_secret(name: str, schema: dict[str, object]) -> bool:
    normalized = name.lower().replace("-", "_")
    return normalized in {
        "access_token",
        "api_key",
        "apikey",
        "client_secret",
        "credential",
        "key",
        "password",
        "private_key",
        "secret",
        "session_cookie",
        "token",
    } or any(
        bool(candidate.get("writeOnly"))
        or candidate.get("format") == "password"
        or candidate.get("x-aggregato-public") is False
        for candidate in _schema_variants(schema)
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
    requested_mode: str | None = None,
    requested_lineage_id: uuid.UUID | None = None,
    preserve_requested_request: bool = False,
    import_path: Path | None = None,
    replay_records: list[RawRecord] | None = None,
    replay_only: bool = False,
    check_restore_status: ProviderStatus | None = None,
    check_restore_enabled: bool | None = None,
) -> RunOutcome:
    """Execute and record one sync run.

    Args:
        engine: The database engine. The child never sees it .
        config: For the provider's configuration and secrets.
        provider_id: Which provider to run.
        mode: ``incremental``, ``full``, or ``import``.
        cursor: Where to resume.
        retry_step: The provider's current ladder position.
        consecutive_failures: Its current failure streak.
        interval_seconds: Its normal interval.
        clock: Time source.
        lineage_id: Set when this run is a retry of earlier work, so the UI groups attempts
            . ``None`` starts a new lineage.
        requested_mode: The durable one-shot request being consumed, if this is its first ordinary
            run. It is consumed atomically with the run row.
        requested_lineage_id: The lineage stored with ``requested_mode``.
        preserve_requested_request: Keep a pending ordinary request intact when an import or replay
            job takes priority for this run.
        import_path: Set only in ``import`` mode.
        replay_records: Explicit failure payloads to normalize in the child.
        replay_only: Do not fetch after replaying ``replay_records``.
        check_restore_status: Provider status to restore after a diagnostic check.
        check_restore_enabled: Whether the provider was enabled before a diagnostic check.

    Returns:
        The run's outcome, already persisted.
    """
    now = clock.now()
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
        requested_mode=requested_mode,
        requested_lineage_id=requested_lineage_id,
    )

    try:
        provider = _provider_info(provider_id, config.provider_dir)
        provider_settings = await _provider_settings(engine, config, provider_id)
        return await _run_opened(
            engine,
            config,
            provider=provider,
            provider_settings=provider_settings,
            provider_id=provider_id,
            run_id=run_id,
            mode=mode,
            cursor=cursor,
            retry_step=retry_step,
            consecutive_failures=consecutive_failures,
            interval_seconds=interval_seconds,
            lineage=lineage,
            clock=clock,
            import_path=import_path,
            replay_records=replay_records,
            replay_only=replay_only,
            now=now,
            preserve_requested_request=preserve_requested_request,
            requested_lineage_id=requested_lineage_id,
            check_restore_status=check_restore_status,
            check_restore_enabled=check_restore_enabled,
        )
    except Exception as exc:
        if mode is FetchMode.CHECK:
            outcome = RunOutcome(
                status=RunStatus.FAILED,
                error_class=ErrorClass.INTERNAL,
                error_message=f"{type(exc).__name__}: {str(exc)[:1000]}",
            )
            await _finalize_check(
                engine,
                provider_id=provider_id,
                run_id=run_id,
                outcome=outcome,
                restore_status=check_restore_status
                or (
                    ProviderStatus.DISABLED
                    if check_restore_enabled is False
                    else ProviderStatus.IDLE
                ),
                now=clock.now(),
            )
            return outcome
        await _finalize_host_failure(
            engine,
            provider_id=provider_id,
            run_id=run_id,
            retry_step=retry_step,
            consecutive_failures=consecutive_failures,
            interval_seconds=interval_seconds,
            now=clock.now(),
            error=exc,
            clock=clock,
            lineage_id=lineage,
            preserve_requested_request=preserve_requested_request,
            requested_lineage_id=requested_lineage_id,
        )
        return RunOutcome(
            status=RunStatus.FAILED,
            error_class=ErrorClass.INTERNAL,
            error_message=f"{type(exc).__name__}: {str(exc)[:1000]}",
        )


async def _run_opened(
    engine: AsyncEngine,
    config: Config,
    *,
    provider: ProviderInfo,
    provider_settings: dict[str, object],
    provider_id: str,
    run_id: int,
    mode: FetchMode,
    cursor: Cursor | None,
    retry_step: int,
    consecutive_failures: int,
    interval_seconds: int,
    lineage: uuid.UUID,
    clock: Clock,
    import_path: Path | None,
    replay_records: list[RawRecord] | None,
    replay_only: bool,
    now: datetime,
    preserve_requested_request: bool,
    requested_lineage_id: uuid.UUID | None,
    check_restore_status: ProviderStatus | None,
    check_restore_enabled: bool | None,
) -> RunOutcome:
    """Execute the post-insertion portion of a run."""
    if mode is FetchMode.CHECK:
        outcome = await execute_run(
            RunRequest(
                provider_id=provider_id,
                mode=FetchMode.CHECK,
                config=_public_provider_settings(provider.config_schema, provider_settings),
                secrets=_secret_provider_settings(provider.config_schema, provider_settings),
                provider_dir=config.provider_dir,
                host_state_dir=config.data_dir / "http-host-state",
            )
        )
        outcome = _validate_check_outcome(outcome)
        await _finalize_check(
            engine,
            provider_id=provider_id,
            run_id=run_id,
            outcome=outcome,
            restore_status=check_restore_status
            or (ProviderStatus.DISABLED if check_restore_enabled is False else ProviderStatus.IDLE),
            now=clock.now(),
        )
        return outcome

    stored_replay_records = (
        replay_records
        if replay_records is not None
        else await records_needing_replay(
            engine, provider_id=provider_id, schema_version=provider.schema_version
        )
    )
    replay_outcome: RunOutcome | None = None
    if stored_replay_records:
        replay_outcome = await execute_run(
            RunRequest(
                provider_id=provider_id,
                mode=mode,
                config=_public_provider_settings(provider.config_schema, provider_settings),
                secrets=_secret_provider_settings(provider.config_schema, provider_settings),
                provider_dir=config.provider_dir,
                host_state_dir=config.data_dir / "http-host-state",
                replay_records=stored_replay_records,
            )
        )
        if replay_outcome.status is RunStatus.SUCCESS:
            await tombstone_replay_derivatives(
                engine,
                provider_id=provider_id,
                native_ids=[record.native_id for record in stored_replay_records],
                now=now,
            )

    if replay_only or (
        replay_outcome is not None and replay_outcome.status is not RunStatus.SUCCESS
    ):
        # A stale-payload replay is part of the run's correctness boundary. Do not fetch new pages,
        # advance the provider schema version, or report success when normalization of a retained
        # payload failed; the payload must remain available for a later retry.
        outcome = replay_outcome or RunOutcome(
            status=RunStatus.SUCCESS,
            error_message="replay job had no retained payload",
        )
    else:
        outcome = await execute_run(
            RunRequest(
                provider_id=provider_id,
                mode=mode,
                cursor=cursor,
                config=_public_provider_settings(provider.config_schema, provider_settings),
                secrets=_secret_provider_settings(provider.config_schema, provider_settings),
                import_path=import_path,
                provider_dir=config.provider_dir,
                host_state_dir=config.data_dir / "http-host-state",
            )
        )

    fetched_count = 0 if replay_only else len(outcome.records) + len(outcome.failures)
    if (
        not replay_only
        and replay_outcome is not None
        and replay_outcome.status is RunStatus.SUCCESS
    ):
        outcome.records = replay_outcome.records + outcome.records
        outcome.failures = replay_outcome.failures + outcome.failures

    written, failed = await _ingest(
        engine, provider, outcome, provider_id=provider_id, run_id=run_id, now=now
    )
    if not replay_only and (replay_outcome is None or replay_outcome.status is RunStatus.SUCCESS):
        await _record_provider_schema_version(
            engine,
            provider_id=provider_id,
            schema_version=provider.schema_version,
            now=now,
        )
    await _apply_full_run_guards(
        engine,
        provider=provider,
        provider_id=provider_id,
        mode=mode,
        outcome=outcome,
        item_count=fetched_count,
        run_started_at=now,
        config=provider_settings,
    )
    await _reschedule(
        engine,
        provider_id=provider_id,
        run_id=run_id,
        outcome=outcome,
        written=written,
        failed=failed,
        retry_step=retry_step,
        consecutive_failures=consecutive_failures,
        interval_seconds=interval_seconds,
        lineage_id=lineage,
        clock=clock,
        preserve_requested_request=preserve_requested_request,
        requested_lineage_id=requested_lineage_id,
    )
    return outcome


def _validate_check_outcome(outcome: RunOutcome) -> RunOutcome:
    """Turn a supervised child result into the diagnostic run classification."""
    if outcome.error_class is not None:
        return outcome
    if outcome.records or outcome.failures or outcome.cursor_after is not None:
        outcome.status = RunStatus.FAILED
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = "credential check emitted non-diagnostic protocol messages"
        return outcome
    result = outcome.check_result
    if result is None:
        outcome.status = RunStatus.FAILED
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = "credential check emitted no result"
        return outcome
    if result.ok:
        if result.error_class is not None:
            outcome.status = RunStatus.FAILED
            outcome.error_class = ErrorClass.INTERNAL
            outcome.error_message = "credential check returned an invalid successful result"
        else:
            outcome.status = RunStatus.SUCCESS
            outcome.error_message = result.detail
        return outcome
    outcome.status = RunStatus.FAILED
    outcome.error_class = result.error_class or ErrorClass.INTERNAL
    outcome.error_message = result.detail or "provider credential check failed"
    return outcome


async def _finalize_check(
    engine: AsyncEngine,
    *,
    provider_id: str,
    run_id: int,
    outcome: RunOutcome,
    restore_status: ProviderStatus,
    now: datetime,
) -> None:
    """Close a diagnostic run without changing sync health, cursor, or schedule state."""
    async with transaction(engine) as conn:
        result = await conn.execute(
            update(sync_runs)
            .where(sync_runs.c.id == run_id, sync_runs.c.status == str(RunStatus.RUNNING))
            .values(
                status=str(outcome.status),
                finished_at=now,
                items_seen=0,
                items_written=0,
                items_failed=0,
                error_class=str(outcome.error_class) if outcome.error_class else None,
                error_message=outcome.error_message,
                log_excerpt=outcome.log_excerpt,
                cursor_after=None,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError(f"check run {run_id} was not open when finalized")
        provider_result = await conn.execute(
            update(providers)
            .where(
                providers.c.id == provider_id,
                providers.c.status == str(ProviderStatus.SYNCING),
            )
            .values(status=str(restore_status), updated_at=now)
        )
        if provider_result.rowcount != 1:
            raise RuntimeError(f"provider {provider_id!r} was not locked by its check")


async def _record_provider_schema_version(
    engine: AsyncEngine, *, provider_id: str, schema_version: int, now: datetime
) -> None:
    async with transaction(engine) as conn:
        await conn.execute(
            update(providers)
            .where(providers.c.id == provider_id)
            .values(schema_version=schema_version, updated_at=now)
        )


async def _finalize_host_failure(
    engine: AsyncEngine,
    *,
    provider_id: str,
    run_id: int,
    retry_step: int,
    consecutive_failures: int,
    interval_seconds: int,
    now: datetime,
    error: BaseException,
    clock: Clock,
    lineage_id: uuid.UUID,
    preserve_requested_request: bool,
    requested_lineage_id: uuid.UUID | None,
) -> None:
    """Close an open run and make its provider retryable in one transaction."""
    message = f"{type(error).__name__}: {str(error)[:1000]}"
    decision = plan_after_failure(
        clock=clock,
        error_class=ErrorClass.INTERNAL,
        retry_step=retry_step,
        consecutive_failures=consecutive_failures,
        normal_interval=timedelta(seconds=interval_seconds),
        lineage_id=lineage_id,
    )
    async with transaction(engine) as conn:
        run_result = await conn.execute(
            update(sync_runs)
            .where(sync_runs.c.id == run_id, sync_runs.c.status == str(RunStatus.RUNNING))
            .values(
                status=str(RunStatus.FAILED),
                finished_at=now,
                error_class=str(ErrorClass.INTERNAL),
                error_message=message,
                items_seen=0,
                items_written=0,
                items_failed=0,
            )
        )
        if run_result.rowcount != 1:
            return
        await conn.execute(
            update(providers)
            .where(
                providers.c.id == provider_id,
                providers.c.enabled.is_(True),
                providers.c.status == str(ProviderStatus.SYNCING),
            )
            .values(
                status=str(decision.status),
                updated_at=now,
                last_error={
                    "error_class": str(ErrorClass.INTERNAL),
                    "message": message,
                    "action_required": action_required(ErrorClass.INTERNAL),
                },
            )
        )
        state_result = await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == provider_id)
            .values(
                next_run_at=decision.next_run_at,
                retry_step=decision.retry_step,
                consecutive_failures=decision.consecutive_failures,
                requested_lineage_id=(requested_lineage_id if preserve_requested_request else None),
            )
        )
        if state_result.rowcount != 1:
            raise RuntimeError(f"provider {provider_id!r} has no scheduling state")


async def _open_run(
    engine: AsyncEngine,
    *,
    provider_id: str,
    lineage_id: uuid.UUID,
    attempt: int,
    mode: FetchMode,
    cursor_before: Cursor | None,
    now: datetime,
    requested_mode: str | None = None,
    requested_lineage_id: uuid.UUID | None = None,
) -> int:
    """Record the run and consume an ordinary request as one atomic state transition."""
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
        if requested_mode is not None:
            request_result = await conn.execute(
                update(provider_state)
                .where(
                    provider_state.c.provider_id == provider_id,
                    provider_state.c.requested_mode == requested_mode,
                    provider_state.c.requested_lineage_id == requested_lineage_id,
                )
                .values(requested_mode=None, requested_lineage_id=None)
            )
            if request_result.rowcount != 1:
                raise RuntimeError(
                    f"requested sync for provider {provider_id!r} changed before run creation"
                )
        # ``run_once`` is also a direct orchestration seam for recovery/import callers and focused
        # tests that do not pass through ``claim``. Make the same durable provider lock explicit
        # here; a scheduler claim that already set ``syncing`` is idempotent.
        provider_admission = providers.c.enabled.is_(True) & providers.c.status.in_(
            (str(ProviderStatus.IDLE), str(ProviderStatus.DEGRADED))
        )
        if mode is FetchMode.CHECK:
            provider_admission = provider_admission | (
                providers.c.enabled.is_(False)
                & providers.c.status.in_(
                    (str(ProviderStatus.DISABLED), str(ProviderStatus.MISCONFIGURED))
                )
            )
        provider_result = await conn.execute(
            update(providers)
            .where(providers.c.id == provider_id, provider_admission)
            .values(status=str(ProviderStatus.SYNCING), updated_at=now)
        )
        running_elsewhere = (
            await conn.execute(
                select(sync_runs.c.id)
                .where(
                    sync_runs.c.provider_id == provider_id,
                    sync_runs.c.status == str(RunStatus.RUNNING),
                    sync_runs.c.id != primary_key[0],
                )
                .limit(1)
            )
        ).first()
        if running_elsewhere is not None:
            raise RuntimeError(f"provider {provider_id!r} already has a running sync")
        if provider_result.rowcount != 1:
            provider_row = (
                await conn.execute(
                    select(providers.c.enabled, providers.c.status).where(
                        providers.c.id == provider_id
                    )
                )
            ).first()
            if (
                provider_row is None
                or provider_row.status != str(ProviderStatus.SYNCING)
                or (mode is not FetchMode.CHECK and not provider_row.enabled)
            ):
                raise RuntimeError(f"provider {provider_id!r} was not admitted for a sync")
        return int(primary_key[0])


async def _ingest(
    engine: AsyncEngine,
    provider: ProviderInfo,
    outcome: RunOutcome,
    *,
    provider_id: str,
    run_id: int,
    now: datetime,
) -> tuple[int, int]:
    """Write what the child produced, in one transaction.

    Called even when the run failed: records that arrived and validated are real, and throwing them
    away would turn a mid-run failure into data loss rather than a partial success .
    """
    scales = list(provider.rating_scales)
    async with transaction(engine) as conn:
        await ensure_rating_scales(conn, scales)
        counts = await write_batches(
            conn,
            WriteContext(
                provider_id=provider_id,
                sync_run_id=run_id,
                schema_version=provider.schema_version,
                now=now,
                rating_scales={scale.id: scale for scale in scales},
            ),
            outcome.records,
        )
        # Records the CHILD could not normalize, stored with their payloads so a fixed provider can
        # replay them . Distinct from records the writer rejected, which write_batches
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
                native_id=failure.native_id,
            )
    return counts.written, counts.failed + len(outcome.failures)


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
    run_id: int,
    outcome: RunOutcome,
    written: int,
    failed: int,
    retry_step: int,
    consecutive_failures: int,
    interval_seconds: int,
    lineage_id: uuid.UUID,
    clock: Clock,
    preserve_requested_request: bool,
    requested_lineage_id: uuid.UUID | None,
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
        run_id=run_id,
        run_status=outcome.status,
        items_seen=len(outcome.records) + len(outcome.failures),
        items_written=written,
        items_failed=failed,
        error_class=outcome.error_class,
        error_message=outcome.error_message,
        log_excerpt=outcome.log_excerpt,
        cursor_after=outcome.cursor_after.state if outcome.cursor_after else None,
        requested_lineage_id=(
            requested_lineage_id if preserve_requested_request else decision.lineage_id
        ),
    )


async def _apply_full_run_guards(
    engine: AsyncEngine,
    *,
    provider: ProviderInfo,
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
        if not result.passed:
            await conn.execute(
                update(provider_state)
                .where(provider_state.c.provider_id == provider_id)
                .values(last_failed_window_item_count=item_count)
            )
            outcome.status = RunStatus.PARTIAL
            outcome.error_class = ErrorClass.PARSE
            outcome.error_message = (
                f"full fetch returned {item_count} items, below the sanity threshold "
                f"of {result.minimum_count} from the prior window"
            )
            return False
        # Commit the observation as the new comparison point only after every sanity guard has
        # passed. A rejected 100 -> 1 window must not make the next 1-item truncation look healthy.
        await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == provider_id)
            .values(last_window_item_count=item_count)
        )
        # A full fetch regenerated every ambiguity this provider still emits. Preserve old queue
        # rows for audit, but close them so historical imports cannot keep presenting stale
        # candidates or duplicate decisions to the operator.
        await supersede_stale_open_items(
            conn,
            provider_id=provider_id,
            refreshed_at=run_started_at,
            now=run_started_at,
        )
        capabilities = set(provider.capabilities)
        if (
            bool(config.get("infer_deletes", False))
            and Capability.REPORTS_DELETES.value not in capabilities
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
