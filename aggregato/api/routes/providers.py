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
from urllib.parse import urlsplit

from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
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

    return [
        _view(info, rows.get(info.id), config)
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
        exists = (await conn.execute(select(providers.c.id).where(providers.c.id == id))).first()
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
                        kv={},
                    )
                )

    rows = await _provider_rows(engine)
    return _view(info, rows.get(id), config)


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
                .where(sync_runs.c.provider_id == id)
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
        inferred_settings = await _settings_inferred_from_import(
            info,
            stored.temporary,
            enabled=not any(
                setting.startswith(f"providers.{id}.") for setting in config.file_pinned
            ),
        )
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
            if inferred_settings:
                stored_config = (
                    await conn.execute(select(providers.c.config).where(providers.c.id == id))
                ).scalar_one_or_none()
                merged_settings = dict(stored_config) if isinstance(stored_config, dict) else {}
                # A value chosen explicitly in the UI always wins over a value discoverable in an
                # export. This also avoids replacing an explicit RSS URL with a derived username
                # URL.
                for key, value in inferred_settings.items():
                    merged_settings.setdefault(key, value)
                await conn.execute(
                    update(providers)
                    .where(providers.c.id == id)
                    .values(config=merged_settings, updated_at=now)
                )
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


async def _settings_inferred_from_import(
    info: ProviderInfo, path: Path, *, enabled: bool
) -> dict[str, object]:
    """Run a host-owned, manifest-selected metadata parser.

    This best-effort step exists solely for optional settings, such as a public feed's account name.
    It never imports or calls provider code. File-pinned settings remain authoritative and are never
    copied into the database.
    """
    if not enabled or info.import_inference != "letterboxd_rss_username":
        return {}
    try:
        payload = await asyncio.to_thread(path.read_bytes)
        settings = _infer_letterboxd_username(payload)
    except (OSError, ET.ParseError):
        return {}
    return {"username": settings} if settings is not None else {}


def _infer_letterboxd_username(payload: bytes) -> str | None:
    """Read only the public account link from a Letterboxd RSS export."""
    root = ET.fromstring(payload)
    link = root.findtext("./channel/link")
    if not isinstance(link, str) or not link:
        return None
    parsed = urlsplit(link.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "letterboxd.com",
        "www.letterboxd.com",
    }:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9_]+", parts[0]) else None


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
                providers.c.config,
            ).join(
                provider_state,
                provider_state.c.provider_id == providers.c.id,
                isouter=True,
            )
        )
        return {row.id: row for row in result}


def _view(info: ProviderInfo, row: Any | None, config: Config) -> ProviderView:
    """Merge a provider's declaration with its database state into the contract's shape."""
    pinned = sorted(p for p in config.file_pinned if p.startswith(f"providers.{info.id}."))
    interval = int(info.default_poll_interval.total_seconds())
    stored_settings = getattr(row, "config", None) if row is not None else None
    settings = stored_settings if isinstance(stored_settings, dict) and stored_settings else None
    if settings is None:
        fallback = config.providers.get(info.id)
        settings = fallback.settings if fallback is not None else {}
    current_settings = _public_settings(info.config_schema, settings)

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
    )


def _legacy_public_settings(model: type[BaseModel], settings: dict[str, Any]) -> dict[str, Any]:
    """Return only configuration values that are explicitly safe to send to the browser.

    Provider settings can contain credentials.  The provider-owned JSON Schema is the authority:
    unknown keys are omitted, as are ``writeOnly``/password fields and conventionally named
    credential fields.  Recursing through declared object fields keeps that guarantee true for
    future nested provider settings too.
    """
    schema = model.model_json_schema()
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return {}
    public: dict[str, Any] = {}
    for key, value in settings.items():
        field_schema = properties.get(key)
        if not isinstance(field_schema, dict) or _is_sensitive_setting(key, field_schema):
            continue
        public[key] = _public_value(value, field_schema)
    return public


def _public_value(value: Any, schema: dict[str, Any]) -> Any:
    """Remove secret descendants from a value described by one JSON Schema node."""
    properties = schema.get("properties")
    if isinstance(value, dict) and isinstance(properties, dict):
        return {
            key: _public_value(child, child_schema)
            for key, child in value.items()
            if isinstance(key, str)
            and isinstance(child_schema := properties.get(key), dict)
            and not _is_sensitive_setting(key, child_schema)
        }
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_public_value(item, schema["items"]) for item in value]
    return value


def _is_sensitive_setting(name: str, schema: dict[str, Any]) -> bool:
    """Recognise both schema-marked secrets and defensively named credential fields."""
    normalized = name.lower().replace("-", "_")
    sensitive_names = {"api_key", "apikey", "key", "password", "secret", "token", "credential"}
    return (
        bool(schema.get("writeOnly"))
        or schema.get("format") == "password"
        or normalized in sensitive_names
    )


def _public_settings(schema: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Project settings through the manifest schema without exposing secret descendants."""
    public: dict[str, Any] = {}
    properties = _schema_properties(schema, schema)
    for key, value in settings.items():
        node = properties.get(key)
        if not isinstance(node, dict) or _schema_is_sensitive(key, node, schema):
            continue
        public[key] = _public_schema_value(value, node, schema)
    return public


def _public_schema_value(value: Any, node: dict[str, Any], root: dict[str, Any]) -> Any:
    variants = _schema_variants(node, root)
    if isinstance(value, dict):
        properties: dict[str, dict[str, Any]] = {}
        for variant in variants:
            properties.update(_schema_properties(variant, root))
        return {
            key: _public_schema_value(child, child_schema, root)
            for key, child in value.items()
            if isinstance(key, str)
            and isinstance(child_schema := properties.get(key), dict)
            and not _schema_is_sensitive(key, child_schema, root)
        }
    if isinstance(value, list):
        item_schema = next(
            (
                variant.get("items")
                for variant in variants
                if isinstance(variant.get("items"), dict)
            ),
            None,
        )
        if isinstance(item_schema, dict):
            return [_public_schema_value(item, item_schema, root) for item in value]
    return value


def _schema_is_sensitive(name: str, node: dict[str, Any], root: dict[str, Any]) -> bool:
    normalized = name.lower().replace("-", "_")
    sensitive_names = {
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
    }
    variants = _schema_variants(node, root)
    if normalized in sensitive_names:
        return True
    if any(
        bool(variant.get("writeOnly"))
        or variant.get("format") == "password"
        or variant.get("x-aggregato-public") is False
        for variant in variants
    ):
        return True
    # Public settings are an explicit manifest allowlist. This prevents a drop-in schema from
    # accidentally reflecting a credential merely because its author chose a non-obvious name.
    return not any(variant.get("x-aggregato-public") is True for variant in variants)


def _schema_variants(node: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(ref := node.get("$ref"), str) and ref.startswith("#/"):
        resolved: Any = root
        for part in ref[2:].split("/"):
            if not isinstance(resolved, dict):
                return []
            resolved = resolved.get(part.replace("~1", "/").replace("~0", "~"))
        return _schema_variants(resolved, root) if isinstance(resolved, dict) else []
    variants = [node]
    for key in ("anyOf", "oneOf", "allOf"):
        for child in node.get(key, []):
            if isinstance(child, dict):
                variants.extend(_schema_variants(child, root))
    return variants


def _schema_properties(node: dict[str, Any], root: dict[str, Any]) -> dict[str, dict[str, Any]]:
    properties: dict[str, dict[str, Any]] = {}
    for variant in _schema_variants(node, root):
        value = variant.get("properties")
        if isinstance(value, dict):
            properties.update(
                {key: child for key, child in value.items() if isinstance(child, dict)}
            )
    return properties


def _validate_settings(schema: dict[str, Any], value: object) -> list[str]:
    """Validate the manifest subset needed by the API without importing provider code."""
    errors = _validate_schema_node(value, schema, schema, path="settings")
    return errors


def _validate_schema_node(
    value: object, node: dict[str, Any], root: dict[str, Any], *, path: str
) -> list[str]:
    resolved = _resolve_schema(node, root)
    if resolved is None:
        return [f"{path} references an unknown schema"]

    choices = [
        child
        for key in ("anyOf", "oneOf")
        for child in resolved.get(key, [])
        if isinstance(child, dict)
    ]
    if choices:
        matches = [not _validate_schema_node(value, child, root, path=path) for child in choices]
        if ("oneOf" in resolved and sum(matches) != 1) or (
            "anyOf" in resolved and not any(matches)
        ):
            return [f"{path} does not match the provider schema"]
        return []

    all_of = [child for child in resolved.get("allOf", []) if isinstance(child, dict)]
    all_errors = [
        error for child in all_of for error in _validate_schema_node(value, child, root, path=path)
    ]
    if all_errors:
        return all_errors

    # Validate constraints declared alongside an allOf composition as well, without recursing
    # back into the same composition.
    node_without_composition = {
        key: value for key, value in resolved.items() if key not in {"anyOf", "oneOf", "allOf"}
    }
    expected = node_without_composition.get("type")
    if isinstance(expected, list):
        expected_types = {item for item in expected if isinstance(item, str)}
    else:
        expected_types = {expected} if isinstance(expected, str) else set()
    if expected_types and not _matches_json_type(value, expected_types):
        return [f"{path} has an invalid type"]
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path} must be an object"]
        properties = _schema_properties(node_without_composition, root)
        required = {
            item for item in node_without_composition.get("required", []) if isinstance(item, str)
        }
        errors = [f"{path}.{key} is required" for key in sorted(required) if key not in value]
        if node_without_composition.get("additionalProperties") is False:
            errors.extend(
                f"{path}.{key} is not a recognized setting"
                for key in value
                if key not in properties
            )
        for key, child in value.items():
            if isinstance(key, str) and isinstance(child_schema := properties.get(key), dict):
                errors.extend(
                    _validate_schema_node(child, child_schema, root, path=f"{path}.{key}")
                )
        return errors
    if expected == "array":
        if not isinstance(value, list):
            return [f"{path} must be an array"]
        item_schema = node_without_composition.get("items")
        if isinstance(item_schema, dict):
            return [
                error
                for index, item in enumerate(value)
                for error in _validate_schema_node(item, item_schema, root, path=f"{path}[{index}]")
            ]
    elif expected == "string":
        if not isinstance(value, str):
            return [f"{path} must be a string"]
        minimum = node_without_composition.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            return [f"{path} must contain at least {minimum} characters"]
    elif expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return [f"{path} must be an integer"]
    elif expected == "number" and (not isinstance(value, int | float) or isinstance(value, bool)):
        return [f"{path} must be a number"]
    elif expected == "boolean" and not isinstance(value, bool):
        return [f"{path} must be a boolean"]
    elif expected == "null" and value is not None:
        return [f"{path} must be null"]
    return []


def _resolve_schema(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any] | None:
    reference = node.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return node
    resolved: Any = root
    for part in reference[2:].split("/"):
        if not isinstance(resolved, dict):
            return None
        resolved = resolved.get(part.replace("~1", "/").replace("~0", "~"))
    return resolved if isinstance(resolved, dict) else None


def _matches_json_type(value: object, expected: set[str]) -> bool:
    return any(
        (kind == "object" and isinstance(value, dict))
        or (kind == "array" and isinstance(value, list))
        or (kind == "string" and isinstance(value, str))
        or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
        or (kind == "boolean" and isinstance(value, bool))
        or (kind == "null" and value is None)
        for kind in expected
    )


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything is stored UTC (data-model.md)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
                    )
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
    return _view(info, rows.get(provider_id), config)
