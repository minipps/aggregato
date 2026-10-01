"""Own the parent-side lifecycle for one provider run.

Dispatch opens the run row, supervises the child, ingests returned records, and releases the
provider through the scheduler. Records produced before a child failure are still ingested, while
the cursor advances only to a checkpoint the child flushed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config
from aggregato.db.engine import transaction
from aggregato.db.schema import provider_state, providers, sync_runs
from aggregato.domain.clock import SYSTEM_CLOCK, Clock
from aggregato.domain.enums import (
    Capability,
    ErrorClass,
    FetchMode,
    IngestStage,
    ProviderStatus,
    RunPhase,
    RunStatus,
)
from aggregato.domain.models import Cursor, RawRecord
from aggregato.ingest.failures import capture_failure
from aggregato.ingest.normalize_replay import records_needing_replay
from aggregato.ingest.resolve_queue import supersede_stale_open_items
from aggregato.ingest.writer import WriteContext, ensure_rating_scales, infer_deletes, write_batches
from aggregato.providers.registry import ProviderInfo, discover_providers
from aggregato.sync.errors import action_required, schedules_retry
from aggregato.sync.jobs import (
    claim_import,
    claim_replay,
    fail_import,
    fail_replay,
    finish_import,
    finish_replay,
)
from aggregato.sync.progress import RunProgress
from aggregato.sync.retry import plan_after_failure, plan_after_success
from aggregato.sync.runner import (
    MAX_PAYLOAD_BYTES,
    MAX_REPLAY_RECORDS,
    RunOutcome,
    RunRequest,
    execute_run,
)
from aggregato.sync.sanity import assess_window
from aggregato.sync.scheduler import DueProvider, release

log = logging.getLogger(__name__)

# Keep each replay child well below its protocol caps so a large archive spans committed batches.
_REPLAY_BATCH_RECORDS = min(1_000, MAX_REPLAY_RECORDS)


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
        job = await claim_import(engine, due.provider_id, now=now)
        replay_job = (
            None if job is not None else await claim_replay(engine, due.provider_id, now=now)
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
            job.lineage_id
            if job is not None and job.lineage_id is not None
            else replay_job.lineage_id
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
                import_path=job.path if job is not None else None,
                replay_records=[replay_job.record] if replay_job is not None else None,
                replay_only=replay_job is not None,
                check_restore_status=due.status if requested_mode == str(FetchMode.CHECK) else None,
                check_restore_enabled=due.enabled
                if requested_mode == str(FetchMode.CHECK)
                else None,
            )
        except Exception as exc:
            if job is not None:
                await fail_import(engine, job, error=exc, now=clock.now())
            if replay_job is not None:
                await fail_replay(engine, replay_job, error=exc, now=clock.now())
            raise
        if job is not None:
            if outcome.status is RunStatus.SUCCESS:
                finished = await finish_import(engine, job, now=clock.now())
                if finished:
                    await _remove_import_file(job.path)
            else:
                await fail_import(
                    engine,
                    job,
                    error=RuntimeError(outcome.error_message or "import did not complete"),
                    now=clock.now(),
                )
        if replay_job is not None:
            if outcome.status is RunStatus.SUCCESS and not outcome.failures:
                await finish_replay(engine, replay_job, now=clock.now())
            else:
                await fail_replay(
                    engine,
                    replay_job,
                    error=RuntimeError(outcome.error_message or "replay did not complete"),
                    now=clock.now(),
                )

    return dispatch


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
        engine: The database engine. The child never sees it.
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
    except asyncio.CancelledError as exc:
        try:
            if mode is FetchMode.CHECK:
                await _finalize_check(
                    engine,
                    provider_id=provider_id,
                    run_id=run_id,
                    outcome=RunOutcome(
                        status=RunStatus.FAILED,
                        error_class=ErrorClass.INTERNAL,
                        error_message="diagnostic check was cancelled",
                    ),
                    restore_status=check_restore_status
                    or (
                        ProviderStatus.DISABLED
                        if check_restore_enabled is False
                        else ProviderStatus.IDLE
                    ),
                    restore_enabled=check_restore_enabled,
                    now=clock.now(),
                )
            else:
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
        except Exception:
            log.exception("could not finalize cancelled sync run %s", run_id)
        raise
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
                restore_enabled=check_restore_enabled,
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
    public_settings = _public_provider_settings(provider.config_schema, provider_settings)
    secret_settings = _secret_provider_settings(provider.config_schema, provider_settings)
    request = RunRequest(
        provider_id=provider_id,
        mode=mode,
        config=public_settings,
        secrets=secret_settings,
        provider_dir=config.provider_dir,
        host_state_dir=config.data_dir / "http-host-state",
    )
    progress = RunProgress(engine=engine, run_id=run_id, clock=clock)
    if mode is FetchMode.CHECK:
        await progress.set_phase(RunPhase.CHECKING)
        outcome = await execute_run(
            replace(request, mode=FetchMode.CHECK), on_message=progress.observe
        )
        outcome = _validate_check_outcome(outcome)
        await _finalize_check(
            engine,
            provider_id=provider_id,
            run_id=run_id,
            outcome=outcome,
            restore_status=check_restore_status
            or (ProviderStatus.DISABLED if check_restore_enabled is False else ProviderStatus.IDLE),
            restore_enabled=check_restore_enabled,
            now=clock.now(),
        )
        return outcome

    replay_written = 0
    replay_failed = 0
    replay_seen = 0
    replay_skipped = 0
    replay_problem: RunOutcome | None = None

    async def ingest_replay_batch(records: list[RawRecord]) -> RunOutcome | None:
        nonlocal replay_written, replay_failed, replay_seen
        child_outcome = await execute_run(
            replace(request, replay_records=records), on_message=progress.observe
        )
        expected: Counter[str | None] = Counter(record.native_id for record in records)
        observed: Counter[str | None] = Counter(
            record.native_id for record, _batch in child_outcome.records
        )
        observed.update(failure.native_id for failure in child_outcome.failures)
        has_unexpected = any(count > expected[native_id] for native_id, count in observed.items())
        if has_unexpected or (child_outcome.status is RunStatus.SUCCESS and observed != expected):
            return RunOutcome(
                status=RunStatus.FAILED,
                error_class=ErrorClass.INTERNAL,
                error_message="replay child did not return one result for each requested record",
            )

        replay_seen += sum(observed.values())
        written, failed = await _ingest(
            engine,
            provider,
            child_outcome,
            provider_id=provider_id,
            run_id=run_id,
            now=now,
            replace_existing=True,
        )
        replay_written += written
        replay_failed += failed
        await progress.record_ingest(items_written=replay_written, items_failed=replay_failed)
        return child_outcome if child_outcome.status is not RunStatus.SUCCESS else None

    if replay_records is not None:
        if replay_records:
            await progress.set_phase(RunPhase.REPLAYING, total=len(replay_records))
            for raw in replay_records:
                replay_problem = await ingest_replay_batch([raw])
                if replay_problem is not None:
                    break
    elif not replay_only:
        await progress.set_phase(RunPhase.REPLAYING)
        fixed_request_bytes = len(request.payload().encode())
        replay_bytes = max(0, MAX_PAYLOAD_BYTES - fixed_request_bytes - 64)
        after_item_id: int | None = None
        while replay_problem is None:
            records, next_item_id, oversized = await records_needing_replay(
                engine,
                provider_id=provider_id,
                schema_version=provider.schema_version,
                after_item_id=after_item_id,
                limit=_REPLAY_BATCH_RECORDS,
                max_record_bytes=replay_bytes,
            )
            replay_skipped += oversized
            if records:
                replay_problem = await ingest_replay_batch(records)
                if replay_problem is not None:
                    break
            if next_item_id is None or next_item_id == after_item_id:
                break
            after_item_id = next_item_id

    if replay_problem is None and (replay_failed or replay_skipped):
        replay_problem = RunOutcome(
            status=RunStatus.PARTIAL,
            error_class=ErrorClass.PARSE,
            error_message=(
                f"normalization replay rejected {replay_failed} record(s)"
                if replay_failed
                else f"normalization replay skipped {replay_skipped} payload(s) over the child "
                "request byte limit"
            ),
        )

    if replay_only and not replay_records:
        outcome = RunOutcome(
            status=RunStatus.FAILED,
            error_class=ErrorClass.PARSE,
            error_message="replay job had no retained payload",
        )
    elif replay_problem is not None:
        # A rejected mapping leaves its old schema version and facts in place. Do not fetch or
        # advance the provider version until every stale payload has a committed replacement.
        outcome = RunOutcome(
            status=(
                replay_problem.status
                if replay_problem.status is not RunStatus.SUCCESS
                else RunStatus.PARTIAL
            ),
            error_class=replay_problem.error_class or ErrorClass.PARSE,
            error_message=replay_problem.error_message
            or f"normalization replay rejected {replay_failed} record(s)",
            log_excerpt=replay_problem.log_excerpt,
            log=replay_problem.log,
            raw_responses=replay_problem.raw_responses,
        )
    elif replay_only:
        outcome = RunOutcome(status=RunStatus.SUCCESS)
    else:
        await progress.set_phase(RunPhase.FETCHING)
        outcome = await execute_run(
            replace(request, cursor=cursor, import_path=import_path),
            on_message=progress.observe,
        )

    fetched_count = (
        0
        if replay_only or replay_problem is not None
        else (len(outcome.records) + len(outcome.failures))
    )
    await progress.set_phase(RunPhase.INGESTING, total=progress.progress_total)
    written, failed = await _ingest(
        engine, provider, outcome, provider_id=provider_id, run_id=run_id, now=now
    )
    await progress.record_ingest(
        items_written=replay_written + written,
        items_failed=replay_failed + failed + replay_skipped,
    )
    if not replay_only and replay_problem is None:
        await _record_provider_schema_version(
            engine,
            provider_id=provider_id,
            schema_version=provider.schema_version,
            now=now,
        )
    await progress.set_phase(RunPhase.FINALIZING, total=progress.progress_total)
    await _apply_full_run_guards(
        engine,
        provider=provider,
        provider_id=provider_id,
        mode=mode,
        outcome=outcome,
        item_count=fetched_count,
        failed_count=replay_failed + failed + replay_skipped,
        run_started_at=now,
        config=provider_settings,
    )
    await _reschedule(
        engine,
        provider_id=provider_id,
        run_id=run_id,
        outcome=outcome,
        written=replay_written + written,
        failed=replay_failed + failed + replay_skipped,
        retry_step=retry_step,
        consecutive_failures=consecutive_failures,
        interval_seconds=interval_seconds,
        lineage_id=lineage,
        clock=clock,
        preserve_requested_request=preserve_requested_request,
        requested_lineage_id=requested_lineage_id,
        items_seen=replay_seen + replay_skipped + fetched_count,
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
    restore_enabled: bool | None,
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
                log=outcome.log,
                raw_responses=outcome.raw_responses,
                cursor_after=None,
                phase=str(
                    RunPhase.FINISHED if outcome.status is RunStatus.SUCCESS else RunPhase.FAILED
                ),
                updated_at=now,
                progress_revision=sync_runs.c.progress_revision + 1,
            )
        )
        if result.rowcount != 1:
            return
        # An operator transition may already have released this provider. The status predicate
        # preserves its latest state rather than overwriting it with the state captured at start.
        await conn.execute(
            update(providers)
            .where(
                providers.c.id == provider_id,
                providers.c.status == str(ProviderStatus.SYNCING),
            )
            .values(
                status=case(
                    (providers.c.enabled.is_(False), str(ProviderStatus.DISABLED)),
                    else_=str(ProviderStatus.IDLE if restore_enabled is False else restore_status),
                ),
                updated_at=now,
            )
        )


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
        current = (
            await conn.execute(
                select(sync_runs.c.items_written).where(
                    sync_runs.c.id == run_id,
                    sync_runs.c.status == str(RunStatus.RUNNING),
                )
            )
        ).first()
        if current is None:
            return
        run_status = (
            RunStatus.PARTIAL
            if isinstance(error, asyncio.CancelledError) and current.items_written > 0
            else RunStatus.FAILED
        )
        run_values: dict[str, object] = {
            "status": str(run_status),
            "finished_at": now,
            "error_class": str(ErrorClass.INTERNAL),
            "error_message": message,
            "phase": str(RunPhase.FINISHED if run_status is RunStatus.PARTIAL else RunPhase.FAILED),
            "updated_at": now,
            "progress_revision": sync_runs.c.progress_revision + 1,
        }
        if isinstance(error, asyncio.CancelledError):
            # Checkpoints describe child output; they are safe only after its records commit.
            run_values["cursor_after"] = None
        run_result = await conn.execute(
            update(sync_runs)
            .where(sync_runs.c.id == run_id, sync_runs.c.status == str(RunStatus.RUNNING))
            .values(run_values)
        )
        if run_result.rowcount != 1:
            return
        await conn.execute(
            update(providers)
            .where(
                providers.c.id == provider_id,
                providers.c.status == str(ProviderStatus.SYNCING),
            )
            .values(
                status=case(
                    (providers.c.enabled.is_(False), str(ProviderStatus.DISABLED)),
                    else_=str(decision.status),
                ),
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
                phase=str(RunPhase.STARTING),
                started_at=now,
                updated_at=now,
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
            claimed_disabled_job = mode in (FetchMode.IMPORT, FetchMode.REPLAY)
            if (
                provider_row is None
                or provider_row.status != str(ProviderStatus.SYNCING)
                or (
                    mode is not FetchMode.CHECK
                    and not claimed_disabled_job
                    and not provider_row.enabled
                )
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
    replace_existing: bool = False,
) -> tuple[int, int]:
    """Write what the child produced, in one transaction.

    Called even when the run failed: records that arrived and validated are real, and throwing them
    away would turn a mid-run failure into data loss rather than a partial success.
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
            replace_existing=replace_existing,
        )
        # Records the CHILD could not normalize, stored with their payloads so a fixed provider can
        # replay them. Distinct from records the writer rejected, which write_batches
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
    items_seen: int | None = None,
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
        provider_id=provider_id,
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
        items_seen=(
            len(outcome.records) + len(outcome.failures) if items_seen is None else items_seen
        ),
        run_id=run_id,
        run_status=outcome.status,
        items_written=written,
        items_failed=failed,
        error_class=outcome.error_class,
        error_message=outcome.error_message,
        log_excerpt=outcome.log_excerpt,
        log=outcome.log,
        raw_responses=outcome.raw_responses,
        cursor_after=outcome.cursor_after.state if outcome.cursor_after else None,
        requested_lineage_id=(
            requested_lineage_id if preserve_requested_request else decision.lineage_id
        ),
        phase=RunPhase.FINISHED if outcome.status is not RunStatus.FAILED else RunPhase.FAILED,
    )


async def _apply_full_run_guards(
    engine: AsyncEngine,
    *,
    provider: ProviderInfo,
    provider_id: str,
    mode: FetchMode,
    outcome: RunOutcome,
    item_count: int,
    failed_count: int = 0,
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
            failed_count == 0
            and not outcome.failures
            and bool(config.get("infer_deletes", False))
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
