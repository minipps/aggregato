"""``/providers`` — discovery, enable/disable, sync now, and latest-run status.

The interesting constraint is what this module may **not** do. An import-linter contract forbids
``aggregato.api -> aggregato.sync``, so "sync now" cannot call the runner. It need not: the schedule
*is* ``provider_state.next_run_at`` (research.md ), so triggering a run means writing that column
and letting the scheduler pick it up on its next poll. The decoupling and the design agree, which is
usually a sign the design was right — a shared table beats a shared process, and it keeps the API
answering while a provider hangs .

Provider ``check`` remains a worker/child contract used by conformance checks; this API deliberately
does not pretend to run it synchronously. The read-only latest-run endpoint reports recorded state,
so a request cannot be mistaken for a credential check or execute extension code in the API.

A discovered provider is inert until explicitly enabled, which is where "a fresh install makes
exactly zero outbound requests"  is either true or not.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Body, File, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.requests import Request

from aggregato.api.clock import now as request_now
from aggregato.api.errors import ProblemError, error_type
from aggregato.config import Config
from aggregato.db.engine import transaction
from aggregato.db.schema import import_jobs, provider_state, providers, sync_runs
from aggregato.domain.enums import (
    Acquisition,
    Capability,
    ErrorClass,
    FetchMode,
    MediaType,
    ProviderStatus,
    RunStatus,
)
from aggregato.providers.registry import ProviderInfo, discover_providers

router = APIRouter(tags=["providers"])


class LastError(BaseModel):
    """The last failure, and what the operator should do about it.

    ``action_required`` is the whole point : a failure that only says "sync failed" costs an
    operator a debugging session, so every error class carries its own instruction ( fills these
    in per class).
    """

    error_class: ErrorClass
    message: str
    action_required: str | None = None


class LastCheck(BaseModel):
    """The latest explicitly requested provider diagnostic, separate from sync health."""

    status: Literal["pending", "success", "failure"]
    lineage_id: uuid.UUID
    requested_at: datetime | None = None
    completed_at: datetime | None = None
    detail: str | None = None
    error_class: ErrorClass | None = None


class ProviderView(BaseModel):
    """One provider, exactly the object ``contracts/openapi.yaml`` declares."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    enabled: bool
    status: ProviderStatus
    acquisition: Acquisition
    #: False for drop-in development providers, which the UI must label ``unreviewed`` .
    reviewed: bool
    capabilities: list[Capability]
    media_types: list[MediaType]
    poll_interval_seconds: int
    next_run_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_error: LastError | None = None
    #: Settings fixed by the config file, which the UI shows as uneditable (research.md ).
    file_pinned_settings: list[str] = Field(default_factory=list)
    #: Configured values that are safe to render.  Write-only and secret fields never leave the API.
    current_settings: dict[str, Any] = Field(default_factory=dict)
    last_check: LastCheck | None = None


class SyncQueued(BaseModel):
    """What ``POST /providers/{id}/sync`` returns: the lineage the attempts will share ."""

    lineage_id: uuid.UUID


class SyncRequest(BaseModel):
    """Validated body for a queued sync request."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["incremental", "full"] = "incremental"


MAX_IMPORT_BYTES = 50 * 1024 * 1024
_IMPORT_QUOTA_LOCK_KEY: Final = 482901736
"""PostgreSQL transaction-lock key shared by every API process admitting an import."""


@dataclass(frozen=True, slots=True)
class StoredImport:
    """A staged upload and the durable path it will receive after admission."""

    path: Path
    temporary: Path
    size: int


class LastRunView(BaseModel):
    """The latest recorded run, without claiming that the API checked credentials."""

    status: RunStatus | None = None
    error_class: ErrorClass | None = None
    detail: str | None = None


@router.get("/providers", response_model=list[ProviderView])
async def list_providers(request: Request) -> list[ProviderView]:
    """Every discovered provider, enabled or not, with its provenance and health.

    Inputs: none beyond authentication.

    Returns: one entry per provider found in the bundled tree. Discovery reads declarations only and
    never runs provider code, so listing a provider cannot cause a request to a platform .
    A provider with no database row yet reports as ``disabled`` with zero failures, which is what a
    fresh install looks like.
    """
    config: Config = request.app.state.config
    engine: AsyncEngine = request.app.state.engine
    rows = await _provider_rows(engine)
    checks = await _check_rows(engine)

    return [
        _view(info, rows.get(info.id), config, checks.get(info.id))
        for info in sorted(discover_providers(config.provider_dir), key=lambda i: i.id)
        if info.api_visible
    ]


@router.post("/providers/{id}/enable", response_model=ProviderView)
async def enable_provider(request: Request, id: str) -> ProviderView:
    """Enable a provider and make it due immediately.

    This is the moment a fresh install stops being silent, so it is deliberately an explicit
    operator action with no default-on path anywhere .

    Failure modes: 404 problem+json if no such provider is installed; 422 if its configuration is
    invalid, in which case it is marked ``misconfigured`` rather than enabled — a broken provider
    never blocks the service or another provider .
    """
    return await _set_enabled(request, id, enabled=True)


@router.post("/providers/{id}/disable", response_model=ProviderView)
async def disable_provider(request: Request, id: str) -> ProviderView:
    """Disable a provider. It keeps its archive and its cursor; it simply stops being due.

    Nothing is deleted — disabling is not a way to lose history .
    """
    return await _set_enabled(request, id, enabled=False)


@router.get("/providers/{id}/config-schema", response_model=dict[str, Any])
async def provider_config_schema(request: Request, id: str) -> dict[str, Any]:
    """Return the provider's declarative settings schema, never its configured values.

    Pydantic produces the schema from the provider's own ``config_model``.  This keeps the API and
    the schema-driven UI independent of provider-specific fields; ``SecretStr`` fields and explicit
    ``writeOnly`` extras survive in the resulting JSON Schema without a host-maintained secret list.
    """
    config: Config = request.app.state.config
    info = _require_installed(id, config.provider_dir)
    return deepcopy(info.config_schema)


@router.put("/providers/{id}/config", response_model=ProviderView)
async def update_provider_config(
    request: Request,
    id: str,
    body: Annotated[dict[str, Any], Body()],
) -> ProviderView:
    """Validate and persist one provider's configuration without enabling it.

    The opaque settings stay in the local database. Responses include only the subset declared
    safe to render by the provider schema; credentials never leave the API. The scheduler reads
    this row for its next run, so this endpoint is a real configuration write rather than UI-only
    state.

    Failure modes: 404 for an unavailable provider; 409 when its settings are pinned by the YAML
    file; 422 when the body does not satisfy the provider's own configuration model.
    """
    config: Config = request.app.state.config
    engine: AsyncEngine = request.app.state.engine
    info = _require_installed(id, config.provider_dir)
    pinned_prefix = f"providers.{id}."
    if any(path.startswith(pinned_prefix) for path in config.file_pinned):
        raise ProblemError(
            status=409,
            title="Provider configuration is file-pinned",
            detail=f"{id} is configured by the mounted YAML file and cannot be changed on the web.",
            type=error_type("provider-config-file-pinned"),
        )
    errors = _validate_settings(info.config_schema, body)
    if errors:
        raise ProblemError(
            status=422,
            title="Provider configuration is invalid",
            detail="; ".join(errors),
            type=error_type("provider-config-invalid"),
        )

    now = request_now(request)
    async with transaction(engine) as conn:
        exists = (
            await conn.execute(
                select(providers.c.id, providers.c.enabled).where(providers.c.id == id)
            )
        ).first()
        if exists is None:
            # Saving settings must not start network activity. Enable is a separate, deliberate
            # operation, preserving the fresh-install silence guarantee .
            await conn.execute(
                providers.insert().values(
                    id=id,
                    enabled=False,
                    status=str(ProviderStatus.DISABLED),
                    acquisition=info.acquisition,
                    schema_version=info.schema_version,
                    reviewed=info.reviewed,
                    config=body,
                    created_at=now,
                    updated_at=now,
                )
            )
            await conn.execute(
                provider_state.insert().values(
                    provider_id=id,
                    effective_interval_seconds=int(info.default_poll_interval.total_seconds()),
                    consecutive_failures=0,
                    retry_step=0,
                    **_now_playing_state_values(info, enabled=False, now=now, fingerprint=None),
                    kv={},
                )
            )
        else:
            await conn.execute(
                update(providers).where(providers.c.id == id).values(config=body, updated_at=now)
            )
            state_exists = (
                await conn.execute(
                    select(provider_state.c.provider_id).where(provider_state.c.provider_id == id)
                )
            ).first()
            if state_exists is None:
                await conn.execute(
                    provider_state.insert().values(
                        provider_id=id,
                        effective_interval_seconds=int(info.default_poll_interval.total_seconds()),
                        consecutive_failures=0,
                        retry_step=0,
                        **_now_playing_state_values(
                            info, enabled=bool(exists.enabled), now=now, fingerprint=None
                        ),
                        kv={},
                    )
                )
            else:
                await conn.execute(
                    update(provider_state)
                    .where(provider_state.c.provider_id == id)
                    .values(
                        **_now_playing_state_values(
                            info, enabled=bool(exists.enabled), now=now, fingerprint=None
                        )
                    )
                )

    rows = await _provider_rows(engine)
    checks = await _check_rows(engine)
    return _view(info, rows.get(id), config, checks.get(id))


@router.post("/providers/{id}/sync", status_code=202, response_model=SyncQueued)
async def sync_now(
    request: Request,
    id: str,
    # Annotated rather than `= Body(...)`: a call in a default is ruff's B008, and this is
    # FastAPI's own current recommendation anyway.
    body: Annotated[SyncRequest | None, Body()] = None,
) -> SyncQueued:
    """Queue a run now by making the provider due.

    The API cannot spawn a run itself — it may not import ``aggregato.sync`` — and does not need to:
    setting ``next_run_at`` to now puts the provider at the front of the due queue and the scheduler
    dispatches it within a poll interval (research.md ). Returns 202 because the run has been
    *queued*, not performed.

    Args:
        request: The request.
        id: Provider id.
        body: Optional ``{"mode": "incremental" | "full"}``. A ``full`` request clears the cursor,
            because that is what "full" means and a full run that resumed from a cursor would not be
            one, and records the mode on ``provider_state.requested_mode`` so the dispatch that
            picks the provider up runs it as ``full`` rather than as the scheduler's incremental
            default — the run's recorded mode and the full-run guards both depend on that.

    Returns:
        The ``lineage_id`` the run and its retries share, so a caller follows the attempts as one
        piece of work .

    Failure modes: 404 if not installed; 409 if a run is already in flight — two concurrent runs of
    one provider would race on its cursor.
    """
    engine: AsyncEngine = request.app.state.engine
    config: Config = request.app.state.config
    _require_installed(id, config.provider_dir)
    mode = (body or SyncRequest()).mode

    now = request_now(request)
    lineage = uuid.uuid4()

    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(providers.c.status, providers.c.enabled).where(providers.c.id == id)
            )
        ).first()
        if row is None or not row.enabled:
            raise ProblemError(
                status=409,
                title="Provider is not enabled",
                detail=f"enable {id} before triggering a sync",
                type=error_type("provider-not-enabled"),
            )
        if row.status == str(ProviderStatus.SYNCING):
            raise ProblemError(
                status=409,
                title="A sync is already running",
                detail=f"{id} is already syncing; two runs would race on its cursor",
                type=error_type("sync-in-flight"),
            )
        if row.status == str(ProviderStatus.MISCONFIGURED):
            raise ProblemError(
                status=409,
                title="Provider configuration is invalid",
                detail=f"fix {id}'s configuration before triggering a sync",
                type=error_type("provider-misconfigured"),
            )

        values: dict[str, Any] = {
            "next_run_at": now,
            "requested_mode": mode,
            "requested_lineage_id": lineage,
        }
        if mode == "full":
            # A "full" run that resumed from a cursor would not be full.
            values["cursor"] = None
        state_result = await conn.execute(
            update(provider_state).where(provider_state.c.provider_id == id).values(values)
        )
        if state_result.rowcount != 1:
            raise ProblemError(
                status=503,
                title="Provider state is incomplete",
                detail="the provider has no scheduling state; repair it before syncing",
                type=error_type("provider-state-missing"),
            )
        # A degraded provider asked to sync gets its ladder reset: the operator has presumably fixed
        # whatever it was complaining about, and refusing to try would be unhelpful .
        provider_result = await conn.execute(
            update(providers)
            .where(
                providers.c.id == id,
                providers.c.enabled.is_(True),
                providers.c.status.in_((str(ProviderStatus.IDLE), str(ProviderStatus.DEGRADED))),
            )
            .values(status=str(ProviderStatus.IDLE), updated_at=now)
        )
        if provider_result.rowcount != 1:
            raise ProblemError(
                status=409,
                title="A sync is already running",
                detail=f"{id} changed state while it was being queued",
                type=error_type("sync-in-flight"),
            )

    return SyncQueued(lineage_id=lineage)


@router.get("/providers/{id}/last-run", response_model=LastRunView)
async def latest_provider_run(request: Request, id: str) -> LastRunView:
    """Report the provider's latest recorded run without contacting the platform.

    A provider that has never run returns a null status with a "not yet known" detail. An actual
    credential check, when needed, belongs in a durable worker request and is intentionally not
    hidden behind this read endpoint.
    """
    engine: AsyncEngine = request.app.state.engine
    config: Config = request.app.state.config
    _require_installed(id, config.provider_dir)

    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(sync_runs.c.status, sync_runs.c.error_class, sync_runs.c.error_message)
                .where(
                    sync_runs.c.provider_id == id,
                    sync_runs.c.mode != str(FetchMode.CHECK),
                )
                .order_by(sync_runs.c.started_at.desc(), sync_runs.c.id.desc())
                .limit(1)
            )
        ).first()

    if row is None:
        return LastRunView(detail="this provider has not run yet")
    status = RunStatus(row.status)
    detail = row.error_message
    if detail is None and status is RunStatus.SUCCESS:
        detail = "last run completed without an error"
    return LastRunView(
        status=status,
        error_class=ErrorClass(row.error_class) if row.error_class else None,
        detail=detail,
    )


@router.post("/providers/{id}/check", status_code=202, response_model=SyncQueued)
async def check_provider(request: Request, id: str) -> SyncQueued:
    """Queue an isolated provider diagnostic without enabling or rescheduling it."""
    config: Config = request.app.state.config
    engine: AsyncEngine = request.app.state.engine
    info = _require_installed(id, config.provider_dir)
    configured = config.providers.get(id)
    now = request_now(request)
    lineage = uuid.uuid4()

    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(
                    providers.c.enabled,
                    providers.c.status,
                    providers.c.config,
                    provider_state.c.requested_mode,
                )
                .select_from(
                    providers.outerjoin(
                        provider_state, provider_state.c.provider_id == providers.c.id
                    )
                )
                .where(providers.c.id == id)
                .with_for_update()
            )
        ).first()
        stored_settings = (
            row.config
            if row is not None and isinstance(row.config, dict) and row.config
            else configured.settings
            if configured is not None
            else {}
        )
        validation_errors = _validate_settings(info.config_schema, stored_settings)
        config_error = configured.error if configured is not None else None
        if config_error is not None or validation_errors:
            raise ProblemError(
                status=422,
                title="Provider configuration is invalid",
                detail=config_error or "; ".join(validation_errors),
                type=error_type("provider-config-invalid"),
            )
        if row is not None and (
            row.status == str(ProviderStatus.SYNCING) or row.requested_mode is not None
        ):
            raise ProblemError(
                status=409,
                title="A provider operation is already pending",
                detail=f"{id} is already syncing or has an explicit request pending",
                type=error_type("provider-operation-in-flight"),
            )
        active = (
            await conn.execute(
                select(sync_runs.c.id)
                .where(
                    sync_runs.c.provider_id == id,
                    sync_runs.c.status == str(RunStatus.RUNNING),
                )
                .limit(1)
            )
        ).first()
        if active is not None:
            raise ProblemError(
                status=409,
                title="A provider operation is already running",
                detail=f"{id} already has a running operation",
                type=error_type("provider-operation-in-flight"),
            )

        if row is None:
            await conn.execute(
                providers.insert().values(
                    id=id,
                    enabled=False,
                    status=str(ProviderStatus.DISABLED),
                    acquisition=info.acquisition,
                    schema_version=info.schema_version,
                    reviewed=info.reviewed,
                    config={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await conn.execute(
                provider_state.insert().values(
                    provider_id=id,
                    effective_interval_seconds=int(info.default_poll_interval.total_seconds()),
                    consecutive_failures=0,
                    retry_step=0,
                    # The request predicate makes this due without changing the ordinary schedule.
                    next_run_at=None,
                    requested_mode=str(FetchMode.CHECK),
                    requested_lineage_id=lineage,
                    kv={},
                )
            )
        else:
            state_result = await conn.execute(
                update(provider_state)
                .where(provider_state.c.provider_id == id)
                .values(
                    requested_mode=str(FetchMode.CHECK),
                    requested_lineage_id=lineage,
                )
            )
            if state_result.rowcount != 1:
                await conn.execute(
                    provider_state.insert().values(
                        provider_id=id,
                        effective_interval_seconds=int(info.default_poll_interval.total_seconds()),
                        consecutive_failures=0,
                        retry_step=0,
                        next_run_at=None,
                        requested_mode=str(FetchMode.CHECK),
                        requested_lineage_id=lineage,
                        kv={},
                    )
                )

    return SyncQueued(lineage_id=lineage)


@router.post("/providers/{id}/import", status_code=202, response_model=SyncQueued)
async def import_file(
    request: Request,
    id: str,
    file: Annotated[UploadFile, File(description="A personal export file for this provider")],
) -> SyncQueued:
    """Persist one operator-supplied export and queue it for the worker.

    The upload is validated before it receives a durable job row, so rejected files leave both the
    archive and queue unchanged.  The API stores opaque bytes; provider-specific structural checks
    happen in the isolated child during the import run.
    """
    config: Config = request.app.state.config
    engine: AsyncEngine = request.app.state.engine
    info = _require_installed(id, config.provider_dir)
    if Capability.FILE_IMPORT.value not in info.capabilities:
        raise ProblemError(
            status=422,
            title="Provider does not accept export files",
            detail=f"{id} does not declare the file_import capability",
            type=error_type("import-not-supported"),
        )
    if not file.filename:
        raise ProblemError(
            status=422,
            title="Missing export file",
            detail="choose a non-empty export file before uploading",
            type=error_type("missing-import-file"),
        )

    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(providers.c.enabled, providers.c.status, provider_state.c.provider_id)
                .join(provider_state, provider_state.c.provider_id == providers.c.id)
                .where(providers.c.id == id)
            )
        ).first()
    if row is None or not row.enabled:
        raise ProblemError(
            status=409,
            title="Provider is not enabled",
            detail=f"enable {id} before importing an export",
            type=error_type("provider-not-enabled"),
        )
    if row.status == str(ProviderStatus.SYNCING):
        raise ProblemError(
            status=409,
            title="A sync is already running",
            detail=f"{id} is already syncing; upload will be accepted after it finishes",
            type=error_type("sync-in-flight"),
        )
    if row.status == str(ProviderStatus.MISCONFIGURED):
        raise ProblemError(
            status=409,
            title="Provider configuration is invalid",
            detail=f"fix {id}'s configuration before importing an export",
            type=error_type("provider-misconfigured"),
        )

    stored = await _store_import(config.data_dir, id, file)
    try:
        lineage = uuid.uuid4()
        now = request_now(request)
        async with transaction(engine) as conn:
            # The first admission read above is only an early rejection. Lock and recheck the
            # provider while claiming the upload, so a scheduler claim that starts between the two
            # reads cannot be overwritten by this import's next_run_at update. PostgreSQL waits
            # for the active claim; SQLite's writer lock gives the same serialize-or-retry shape.
            locked = (
                await conn.execute(
                    select(providers.c.enabled, providers.c.status, provider_state.c.provider_id)
                    .join(provider_state, provider_state.c.provider_id == providers.c.id)
                    .where(providers.c.id == id)
                    .with_for_update()
                )
            ).first()
            if locked is None or not locked.enabled:
                raise ProblemError(
                    status=409,
                    title="Provider is not enabled",
                    detail=f"enable {id} before importing an export",
                    type=error_type("provider-not-enabled"),
                )
            if locked.status == str(ProviderStatus.SYNCING):
                raise ProblemError(
                    status=409,
                    title="A sync is already running",
                    detail=f"{id} is already syncing; upload will be accepted after it finishes",
                    type=error_type("sync-in-flight"),
                )
            if locked.status == str(ProviderStatus.MISCONFIGURED):
                raise ProblemError(
                    status=409,
                    title="Provider configuration is invalid",
                    detail=f"fix {id}'s configuration before importing an export",
                    type=error_type("provider-misconfigured"),
                )
            # This update is both the durable admission claim and the SQLite writer lock. It keeps
            # two uploads for one provider from checking the filesystem quota at the same time;
            # PostgreSQL holds the row lock until this transaction commits, while SQLite serializes
            # the write transaction. The staged .uploading file is deliberately excluded from the
            # usage scan, so add this request's actual byte count explicitly.
            state_update = await conn.execute(
                update(provider_state)
                .where(provider_state.c.provider_id == id)
                .values(next_run_at=now)
            )
            if state_update.rowcount != 1:
                raise ProblemError(
                    status=503,
                    title="Provider state is incomplete",
                    detail="the provider has no scheduling state; repair it before importing",
                    type=error_type("provider-state-missing"),
                )
            await _lock_import_quota(conn)
            used_bytes = await asyncio.to_thread(_import_usage, stored.path.parent)
            provider_total_bytes = used_bytes + stored.size
            if provider_total_bytes > config.api.import_quota_bytes:
                raise ProblemError(
                    status=413,
                    title="Import storage quota exceeded",
                    detail=(
                        f"imports for {id} would use {provider_total_bytes} bytes including this "
                        "upload; "
                        f"the configured quota is {config.api.import_quota_bytes} bytes"
                    ),
                    type=error_type("import-quota-exceeded"),
                )
            total_used_bytes = await asyncio.to_thread(_import_usage, config.data_dir / "imports")
            total_bytes = total_used_bytes + stored.size
            if total_bytes > config.api.import_total_quota_bytes:
                raise ProblemError(
                    status=413,
                    title="Total import storage quota exceeded",
                    detail=(
                        f"imports would use {total_bytes} bytes including this upload; the "
                        f"configured total quota is {config.api.import_total_quota_bytes} bytes"
                    ),
                    type=error_type("import-total-quota-exceeded"),
                )
            await asyncio.to_thread(stored.temporary.replace, stored.path)
            await conn.execute(
                import_jobs.insert().values(
                    provider_id=id,
                    path=str(stored.path),
                    lineage_id=lineage,
                    created_at=now,
                    attempts=0,
                )
            )
    except Exception:
        # The file has no owner until the import_jobs row commits. Do not leave a private orphan
        # behind when the second, race-safe admission check rejects it.
        await asyncio.to_thread(stored.path.unlink, missing_ok=True)
        await asyncio.to_thread(stored.temporary.unlink, missing_ok=True)
        raise
    return SyncQueued(lineage_id=lineage)


async def _store_import(data_dir: Path, provider_id: str, upload: UploadFile) -> StoredImport:
    """Save an upload privately, bounded in size, without trusting its filename as a path."""
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in {".csv", ".rss", ".xml"}:
        raise ProblemError(
            status=422,
            title="Unsupported export file",
            detail="export files must use a .csv, .rss, or .xml extension",
            type=error_type("invalid-import-file"),
        )
    directory = data_dir / "imports" / provider_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{uuid.uuid4()}{suffix}"
    temporary = target.with_suffix(f"{suffix}.uploading")
    total = 0
    try:
        with open(temporary, "xb", buffering=0) as output:  # noqa: ASYNC230
            os.chmod(temporary, 0o600)
            while chunk := await upload.read(64 * 1024):
                total += len(chunk)
                if total > MAX_IMPORT_BYTES:
                    raise ProblemError(
                        status=413,
                        title="Export file is too large",
                        detail=(
                            f"export files may not exceed {MAX_IMPORT_BYTES // (1024 * 1024)} MiB"
                        ),
                        type=error_type("import-too-large"),
                    )
                await asyncio.to_thread(output.write, chunk)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return StoredImport(path=target, temporary=temporary, size=total)


def _import_usage(directory: Path) -> int:
    """Return the private import bytes currently on disk, ignoring temporary uploads."""
    if not directory.is_dir():
        return 0
    total = 0
    for path in directory.rglob("*"):
        if path.is_file() and not path.name.endswith(".uploading"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


async def _lock_import_quota(conn: AsyncConnection) -> None:
    """Serialize total-quota checks across PostgreSQL API processes.

    SQLite already holds its database-wide writer lock from the provider-state ``UPDATE`` in the
    admission transaction. PostgreSQL needs an explicit transaction advisory lock because imports
    for different providers update different state rows.
    """
    if conn.dialect.name == "postgresql":
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _IMPORT_QUOTA_LOCK_KEY},
        )


# --- internals ---------------------------------------------------------------------------------


def _require_installed(provider_id: str, drop_in_dir: Path | None = None) -> ProviderInfo:
    """The provider's declaration, or a 404. Installed is a property of the tree, not the DB."""
    for info in discover_providers(drop_in_dir):
        if info.id == provider_id:
            return info
    raise ProblemError(
        status=404,
        title="No such provider",
        detail=f"{provider_id!r} is not installed",
        type=error_type("provider-not-found"),
    )


async def _provider_rows(engine: AsyncEngine) -> dict[str, Any]:
    """Database state per provider id, for providers that have any."""
    async with transaction(engine) as conn:
        result = await conn.execute(
            select(
                providers.c.id,
                providers.c.enabled,
                providers.c.status,
                providers.c.last_error,
                provider_state.c.next_run_at,
                provider_state.c.last_success_at,
                provider_state.c.consecutive_failures,
                provider_state.c.effective_interval_seconds,
                provider_state.c.requested_mode,
                provider_state.c.requested_lineage_id,
                providers.c.config,
            ).join(
                provider_state,
                provider_state.c.provider_id == providers.c.id,
                isouter=True,
            )
        )
        return {row.id: row for row in result}


async def _check_rows(engine: AsyncEngine) -> dict[str, Any]:
    """Return the newest diagnostic row per provider without mixing it into sync history."""
    async with transaction(engine) as conn:
        result = await conn.execute(
            select(
                sync_runs.c.provider_id,
                sync_runs.c.lineage_id,
                sync_runs.c.status,
                sync_runs.c.started_at,
                sync_runs.c.finished_at,
                sync_runs.c.error_class,
                sync_runs.c.error_message,
            )
            .where(sync_runs.c.mode == str(FetchMode.CHECK))
            .order_by(sync_runs.c.started_at.desc(), sync_runs.c.id.desc())
        )
        rows: dict[str, Any] = {}
        for row in result:
            rows.setdefault(row.provider_id, row)
        return rows


def _view(
    info: ProviderInfo,
    row: Any | None,
    config: Config,
    check_row: Any | None = None,
) -> ProviderView:
    """Merge a provider's declaration with its database state into the contract's shape."""
    pinned = sorted(p for p in config.file_pinned if p.startswith(f"providers.{info.id}."))
    interval = int(info.default_poll_interval.total_seconds())
    stored_settings = getattr(row, "config", None) if row is not None else None
    settings = stored_settings if isinstance(stored_settings, dict) and stored_settings else None
    if settings is None:
        fallback = config.providers.get(info.id)
        settings = fallback.settings if fallback is not None else {}
    current_settings = _public_settings(info.config_schema, settings)
    last_check = _last_check(row, check_row)

    if row is None:
        # Discovered but never enabled: the fresh-install state, and not an error.
        return ProviderView(
            id=info.id,
            name=info.name,
            enabled=False,
            status=ProviderStatus.DISABLED,
            acquisition=Acquisition(info.acquisition),
            reviewed=info.reviewed,
            capabilities=[Capability(c) for c in sorted(info.capabilities)],
            media_types=[MediaType(m) for m in sorted(info.media_types)],
            poll_interval_seconds=interval,
            file_pinned_settings=pinned,
            current_settings=current_settings,
            last_check=last_check,
        )

    return ProviderView(
        id=info.id,
        name=info.name,
        enabled=bool(row.enabled),
        status=ProviderStatus(row.status),
        acquisition=Acquisition(info.acquisition),
        reviewed=info.reviewed,
        capabilities=[Capability(c) for c in sorted(info.capabilities)],
        media_types=[MediaType(m) for m in sorted(info.media_types)],
        poll_interval_seconds=int(row.effective_interval_seconds or interval),
        next_run_at=_aware(row.next_run_at),
        last_success_at=_aware(row.last_success_at),
        consecutive_failures=int(row.consecutive_failures or 0),
        last_error=LastError.model_validate(row.last_error) if row.last_error else None,
        file_pinned_settings=pinned,
        current_settings=current_settings,
        last_check=last_check,
    )


def _last_check(provider_row: Any | None, check_row: Any | None) -> LastCheck | None:
    """Project a pending request or the latest completed check into the provider view."""
    if provider_row is not None and provider_row.requested_mode == str(FetchMode.CHECK):
        lineage = provider_row.requested_lineage_id
        if lineage is not None:
            return LastCheck(
                status="pending",
                lineage_id=lineage,
            )
    if check_row is None:
        return None
    status: Literal["pending", "success", "failure"] = (
        "pending"
        if check_row.status == str(RunStatus.RUNNING)
        else ("success" if check_row.status == str(RunStatus.SUCCESS) else "failure")
    )
    return LastCheck(
        status=status,
        lineage_id=check_row.lineage_id,
        requested_at=_aware(check_row.started_at),
        completed_at=_aware(check_row.finished_at),
        detail=check_row.error_message,
        error_class=ErrorClass(check_row.error_class) if check_row.error_class else None,
    )


@dataclass(frozen=True, slots=True)
class _SettingField:
    """One scalar field from the API's deliberately small provider-schema contract."""

    schema: dict[str, Any]
    nullable: bool
    public: bool


class _UnsupportedProviderSchema(ValueError):
    """A provider schema uses features the API does not implement."""


_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean"})
_ROOT_SCHEMA_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "x-aggregato-public",
    }
)
_FIELD_SCHEMA_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "default",
        "format",
        "writeOnly",
        "x-aggregato-public",
        "enum",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "anyOf",
    }
)


def _setting_fields(schema: object) -> tuple[dict[str, _SettingField], frozenset[str]]:
    """Read the supported flat schema without resolving or traversing JSON Schema."""
    if not isinstance(schema, dict):
        raise _UnsupportedProviderSchema("the schema must be an object")
    _check_schema_keys(schema, _ROOT_SCHEMA_KEYS, "settings")
    if schema.get("type") != "object":
        raise _UnsupportedProviderSchema("settings must be a top-level object")
    if schema.get("additionalProperties") is not False:
        raise _UnsupportedProviderSchema(
            "settings must set additionalProperties to false for a flat schema"
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise _UnsupportedProviderSchema("settings.properties must be an object")

    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
        raise _UnsupportedProviderSchema("settings.required must be a list of field names")
    required_names = frozenset(required)
    unknown_required = sorted(required_names - set(properties))
    if unknown_required:
        raise _UnsupportedProviderSchema(
            f"settings.required names unknown field {unknown_required[0]!r}"
        )

    fields: dict[str, _SettingField] = {}
    for name, node in properties.items():
        if not isinstance(name, str):
            raise _UnsupportedProviderSchema("settings.properties keys must be strings")
        fields[name] = _setting_field(node, f"settings.{name}")
    return fields, required_names


def _setting_field(node: object, path: str) -> _SettingField:
    if not isinstance(node, dict):
        raise _UnsupportedProviderSchema(f"{path} must be a schema object")

    if "anyOf" not in node:
        _validate_scalar_schema(node, path)
        _validate_default(node, node, nullable=False, path=path)
        return _SettingField(node, False, _is_public_field(node, node))

    nullable_keys = {"anyOf", "title", "description", "default", "writeOnly", "x-aggregato-public"}
    _check_schema_keys(node, frozenset(nullable_keys), path)
    choices = node["anyOf"]
    if not isinstance(choices, list) or len(choices) != 2:
        raise _UnsupportedProviderSchema(f"{path} supports only anyOf [scalar, {{'type': 'null'}}]")
    scalar_choices = [
        choice for choice in choices if isinstance(choice, dict) and choice.get("type") != "null"
    ]
    null_choices = [
        choice for choice in choices if isinstance(choice, dict) and choice.get("type") == "null"
    ]
    if len(scalar_choices) != 1 or len(null_choices) != 1:
        raise _UnsupportedProviderSchema(f"{path} supports only anyOf [scalar, {{'type': 'null'}}]")
    if set(null_choices[0]) != {"type"}:
        raise _UnsupportedProviderSchema(
            f"{path} nullable anyOf null branch must be {{'type': 'null'}}"
        )
    scalar = scalar_choices[0]
    _validate_scalar_schema(scalar, path)
    _validate_default(node, scalar, nullable=True, path=path)
    return _SettingField(scalar, True, _is_public_field(node, scalar))


def _validate_scalar_schema(node: dict[str, Any], path: str) -> None:
    kind = node.get("type")
    if kind in {"object", "array"}:
        raise _UnsupportedProviderSchema(
            f"{path} uses unsupported type {kind!r}; nested objects and arrays are not supported"
        )
    _check_schema_keys(node, _FIELD_SCHEMA_KEYS - {"anyOf"}, path)
    if not isinstance(kind, str) or kind not in _SCALAR_TYPES:
        raise _UnsupportedProviderSchema(
            f"{path} must declare one scalar type: string, number, integer, or boolean"
        )

    _validate_field_metadata(node, path)
    enum = node.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise _UnsupportedProviderSchema(f"{path}.enum must be a non-empty list")
        if not all(_matches_scalar_type(value, kind) for value in enum):
            raise _UnsupportedProviderSchema(f"{path}.enum contains a value with the wrong type")

    constraints = {
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    }
    if kind == "string":
        for keyword in ("minLength", "maxLength"):
            value = node.get(keyword)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise _UnsupportedProviderSchema(f"{path}.{keyword} must be a non-negative integer")
        pattern = node.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise _UnsupportedProviderSchema(f"{path}.pattern must be a string")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise _UnsupportedProviderSchema(
                    f"{path}.pattern is not a valid regular expression: {exc}"
                ) from exc
        invalid = constraints - {"minLength", "maxLength", "pattern"}
    elif kind in {"number", "integer"}:
        for keyword in constraints - {"minLength", "maxLength", "pattern"}:
            value = node.get(keyword)
            if value is not None and (
                not isinstance(value, int | float) or isinstance(value, bool)
            ):
                raise _UnsupportedProviderSchema(f"{path}.{keyword} must be a number")
        multiple = node.get("multipleOf")
        if multiple is not None and multiple <= 0:
            raise _UnsupportedProviderSchema(f"{path}.multipleOf must be greater than zero")
        invalid = {"minLength", "maxLength", "pattern"}
    else:
        invalid = constraints
    if any(keyword in node for keyword in invalid):
        raise _UnsupportedProviderSchema(
            f"{path} declares a constraint that does not apply to {kind!r}"
        )


def _validate_field_metadata(node: dict[str, Any], path: str) -> None:
    for keyword in ("title", "description", "format"):
        value = node.get(keyword)
        if value is not None and not isinstance(value, str):
            raise _UnsupportedProviderSchema(f"{path}.{keyword} must be a string")
    for keyword in ("writeOnly", "x-aggregato-public"):
        value = node.get(keyword)
        if value is not None and not isinstance(value, bool):
            raise _UnsupportedProviderSchema(f"{path}.{keyword} must be a boolean")


def _validate_default(
    wrapper: dict[str, Any], scalar: dict[str, Any], *, nullable: bool, path: str
) -> None:
    if "default" not in wrapper:
        return
    value = wrapper["default"]
    if value is None and nullable:
        return
    if not _matches_scalar_type(value, scalar.get("type")):
        raise _UnsupportedProviderSchema(f"{path}.default has the wrong type")


def _is_public_field(wrapper: dict[str, Any], scalar: dict[str, Any]) -> bool:
    """Use only manifest metadata as the redaction policy; absence is private by default."""
    nodes = (wrapper, scalar)
    if any(
        node.get("writeOnly") is True
        or node.get("format") == "password"
        or node.get("x-aggregato-public") is False
        for node in nodes
    ):
        return False
    return any(node.get("x-aggregato-public") is True for node in nodes)


def _check_schema_keys(node: dict[str, Any], allowed: frozenset[str], path: str) -> None:
    unsupported = sorted(key for key in node if key not in allowed)
    if unsupported:
        raise _UnsupportedProviderSchema(
            f"{path} uses unsupported schema keyword {unsupported[0]!r}"
        )


def _public_settings(schema: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Project flat settings through the manifest allowlist without exposing secrets."""
    try:
        fields, _ = _setting_fields(schema)
    except _UnsupportedProviderSchema as exc:
        raise ProblemError(
            status=500,
            title="Provider configuration schema is unsupported",
            detail=str(exc),
            type=error_type("provider-schema-unsupported"),
        ) from exc
    return {
        key: value
        for key, value in settings.items()
        if (field := fields.get(key)) is not None
        and field.public
        and not _validate_field_value(value, field, f"settings.{key}")
    }


def _validate_settings(schema: dict[str, Any], value: object) -> list[str]:
    """Validate flat provider settings without importing provider code or resolving refs."""
    try:
        fields, required = _setting_fields(schema)
    except _UnsupportedProviderSchema as exc:
        return [f"provider configuration schema is unsupported: {exc}"]
    if not isinstance(value, dict):
        return ["settings must be an object"]

    errors = [f"settings.{key} is required" for key in sorted(required) if key not in value]
    errors.extend(
        f"settings.{key} is not a recognized setting" for key in value if key not in fields
    )
    for key, setting in value.items():
        if isinstance(key, str) and (field := fields.get(key)) is not None:
            errors.extend(_validate_field_value(setting, field, f"settings.{key}"))
    return errors


def _validate_field_value(value: object, field: _SettingField, path: str) -> list[str]:
    if value is None:
        return [] if field.nullable else [f"{path} must be {field.schema['type']}"]
    kind = field.schema["type"]
    enum = field.schema.get("enum")
    if kind == "string":
        if not isinstance(value, str):
            return [f"{path} must be a string"]
        if isinstance(enum, list) and not any(value == option for option in enum):
            return [f"{path} must be one of {enum!r}"]
        minimum = field.schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            return [f"{path} must contain at least {minimum} characters"]
        maximum = field.schema.get("maxLength")
        if isinstance(maximum, int) and len(value) > maximum:
            return [f"{path} must contain at most {maximum} characters"]
        pattern = field.schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            return [f"{path} does not match the provider pattern"]
    elif kind in {"number", "integer"}:
        if not isinstance(value, int | float) or isinstance(value, bool):
            message = "an integer" if kind == "integer" else "a number"
            return [f"{path} must be {message}"]
        if kind == "integer" and not isinstance(value, int):
            return [f"{path} must be an integer"]
        if isinstance(enum, list) and not any(value == option for option in enum):
            return [f"{path} must be one of {enum!r}"]
        minimum = field.schema.get("minimum")
        if isinstance(minimum, int | float) and not isinstance(minimum, bool) and value < minimum:
            return [f"{path} must be at least {minimum}"]
        maximum = field.schema.get("maximum")
        if isinstance(maximum, int | float) and not isinstance(maximum, bool) and value > maximum:
            return [f"{path} must be at most {maximum}"]
        exclusive_minimum = field.schema.get("exclusiveMinimum")
        if (
            isinstance(exclusive_minimum, int | float)
            and not isinstance(exclusive_minimum, bool)
            and value <= exclusive_minimum
        ):
            return [f"{path} must be greater than {exclusive_minimum}"]
        exclusive_maximum = field.schema.get("exclusiveMaximum")
        if (
            isinstance(exclusive_maximum, int | float)
            and not isinstance(exclusive_maximum, bool)
            and value >= exclusive_maximum
        ):
            return [f"{path} must be less than {exclusive_maximum}"]
        multiple = field.schema.get("multipleOf")
        if isinstance(multiple, int | float) and not isinstance(multiple, bool):
            quotient = value / multiple
            if abs(quotient - round(quotient)) > 1e-9:
                return [f"{path} must be a multiple of {multiple}"]
    elif kind == "boolean":
        if not isinstance(value, bool):
            return [f"{path} must be a boolean"]
        if isinstance(enum, list) and not any(value == option for option in enum):
            return [f"{path} must be one of {enum!r}"]
    else:
        return [f"{path} has unsupported type {kind!r}"]
    return []


def _matches_scalar_type(value: object, kind: object) -> bool:
    return (
        (kind == "string" and isinstance(value, str))
        or (kind == "number" and isinstance(value, int | float) and not isinstance(value, bool))
        or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (kind == "boolean" and isinstance(value, bool))
    )


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything is stored UTC (data-model.md)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _now_playing_state_values(
    info: ProviderInfo,
    *,
    enabled: bool,
    now: datetime,
    fingerprint: str | None,
) -> dict[str, object]:
    """Reset transient presence atomically with a provider lifecycle write."""
    return {
        "now_playing_item": None,
        "now_playing_changed_at": None,
        "now_playing_checked_at": None,
        "now_playing_next_poll_at": now
        if enabled and Capability.NOW_PLAYING.value in info.capabilities
        else None,
        "now_playing_failures": 0,
        "now_playing_config_fingerprint": fingerprint,
    }


async def _set_enabled(request: Request, provider_id: str, *, enabled: bool) -> ProviderView:
    """Enable or disable, creating the provider's rows on first enable."""
    config: Config = request.app.state.config
    engine: AsyncEngine = request.app.state.engine
    info = _require_installed(provider_id, config.provider_dir)

    provider_config = config.providers.get(provider_id)
    now = request_now(request)

    async with transaction(engine) as conn:
        existing = (
            await conn.execute(
                select(providers.c.id, providers.c.config).where(providers.c.id == provider_id)
            )
        ).first()
        stored_settings = (
            existing.config
            if existing is not None and isinstance(existing.config, dict) and existing.config
            else provider_config.settings
            if provider_config is not None
            else {}
        )
        validation_errors = (
            _validate_settings(info.config_schema, stored_settings) if enabled else []
        )
        config_error = provider_config.error if provider_config is not None else None
        misconfigured = enabled and (config_error is not None or bool(validation_errors))
        status = (
            ProviderStatus.MISCONFIGURED
            if misconfigured
            else (ProviderStatus.IDLE if enabled else ProviderStatus.DISABLED)
        )
        stored_enabled = enabled and not misconfigured
        error_message = config_error or "; ".join(validation_errors)
        if existing is None:
            await conn.execute(
                providers.insert().values(
                    id=provider_id,
                    enabled=stored_enabled,
                    status=str(status),
                    acquisition=info.acquisition,
                    schema_version=info.schema_version,
                    reviewed=info.reviewed,
                    config={},
                    last_error=(
                        {
                            "error_class": str(ErrorClass.INTERNAL),
                            "message": error_message or "provider configuration is invalid",
                            "action_required": "fix this provider's configuration and enable again",
                        }
                        if misconfigured
                        else None
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )
            await conn.execute(
                provider_state.insert().values(
                    provider_id=provider_id,
                    effective_interval_seconds=int(info.default_poll_interval.total_seconds()),
                    consecutive_failures=0,
                    retry_step=0,
                    # Due immediately on first enable, so an operator who just enabled a platform
                    # sees something happen rather than waiting out an interval.
                    next_run_at=(
                        now
                        if enabled
                        and not misconfigured
                        and Capability.POLL.value in info.capabilities
                        else None
                    ),
                    **_now_playing_state_values(
                        info,
                        enabled=stored_enabled,
                        now=now,
                        fingerprint=None,
                    ),
                    kv={},
                )
            )
        else:
            values: dict[str, object] = {
                "enabled": stored_enabled,
                "status": str(status),
                "updated_at": now,
            }
            if enabled:
                values["last_error"] = (
                    {
                        "error_class": str(ErrorClass.INTERNAL),
                        "message": error_message or "provider configuration is invalid",
                        "action_required": "fix this provider's configuration and enable again",
                    }
                    if misconfigured
                    else None
                )
            await conn.execute(
                update(providers).where(providers.c.id == provider_id).values(values)
            )
            state_result = await conn.execute(
                update(provider_state)
                .where(provider_state.c.provider_id == provider_id)
                .values(
                    next_run_at=(
                        now
                        if enabled
                        and not misconfigured
                        and Capability.POLL.value in info.capabilities
                        else None
                    ),
                    **_now_playing_state_values(
                        info,
                        enabled=stored_enabled,
                        now=now,
                        fingerprint=None,
                    ),
                )
            )
            if state_result.rowcount != 1:
                await conn.execute(
                    provider_state.insert().values(
                        provider_id=provider_id,
                        effective_interval_seconds=int(info.default_poll_interval.total_seconds()),
                        consecutive_failures=0,
                        retry_step=0,
                        next_run_at=(
                            now
                            if enabled
                            and not misconfigured
                            and Capability.POLL.value in info.capabilities
                            else None
                        ),
                        **_now_playing_state_values(
                            info,
                            enabled=stored_enabled,
                            now=now,
                            fingerprint=None,
                        ),
                        kv={},
                    )
                )

    if misconfigured:
        raise ProblemError(
            status=422,
            title="Provider configuration is invalid",
            detail=(
                f"{provider_id} is marked misconfigured and was not enabled: "
                f"{error_message or 'unknown error'}"
            ),
            type=error_type("provider-misconfigured"),
        )

    rows = await _provider_rows(engine)
    checks = await _check_rows(engine)
    return _view(info, rows.get(provider_id), config, checks.get(provider_id))
