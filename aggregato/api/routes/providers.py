"""``/providers`` — discovery, enable/disable, sync now, credential check (T056).

The interesting constraint is what this module may **not** do. An import-linter contract forbids
``aggregato.api -> aggregato.sync``, so "sync now" cannot call the runner. It need not: the schedule
*is* ``provider_state.next_run_at`` (research.md R1), so triggering a run means writing that column
and letting the scheduler pick it up on its next poll. The decoupling and the design agree, which is
usually a sign the design was right — a shared table beats a shared process, and it keeps the API
answering while a provider hangs (FR-025).

The same reasoning applies to ``check``: verifying credentials means running provider code, which
happens in a child process the scheduler owns. So the endpoint records a *request* for a check and
reports the last result, rather than blocking a request thread on a third-party platform.

A discovered provider is inert until explicitly enabled, which is where "a fresh install makes
exactly zero outbound requests" (SC-013) is either true or not.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, UploadFile
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

from aggregato.api.errors import ProblemError, error_type
from aggregato.config import Config
from aggregato.db.engine import transaction
from aggregato.db.schema import import_jobs, provider_state, providers, sync_runs
from aggregato.domain.enums import Acquisition, Capability, ErrorClass, MediaType, ProviderStatus
from aggregato.providers.registry import ProviderInfo, discover_providers, load_provider

router = APIRouter(tags=["providers"])


class LastError(BaseModel):
    """The last failure, and what the operator should do about it.

    ``action_required`` is the whole point (SC-005): a failure that only says "sync failed" costs an
    operator a debugging session, so every error class carries its own instruction (T090 fills these
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
    #: False for drop-in development providers, which the UI must label ``unreviewed`` (FR-041).
    reviewed: bool
    capabilities: list[Capability]
    media_types: list[MediaType]
    poll_interval_seconds: int
    next_run_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_error: LastError | None = None
    #: Settings fixed by the config file, which the UI shows as uneditable (research.md R15).
    file_pinned_settings: list[str] = []


class SyncQueued(BaseModel):
    """What ``POST /providers/{id}/sync`` returns: the lineage the attempts will share (FR-019)."""

    lineage_id: uuid.UUID


MAX_IMPORT_BYTES = 50 * 1024 * 1024


class CheckResultView(BaseModel):
    """The last credential-check outcome, per the contract's inline schema."""

    ok: bool
    error_class: ErrorClass | None = None
    detail: str | None = None


@router.get("/providers", response_model=list[ProviderView])
async def list_providers(request: Request) -> list[ProviderView]:
    """Every discovered provider, enabled or not, with its provenance and health.

    Inputs: none beyond authentication.

    Returns: one entry per provider found in the bundled tree. Discovery reads declarations only and
    never runs provider code, so listing a provider cannot cause a request to a platform (SC-013).
    A provider with no database row yet reports as ``disabled`` with zero failures, which is what a
    fresh install looks like.
    """
    config: Config = request.app.state.config
    engine: AsyncEngine = request.app.state.engine
    rows = await _provider_rows(engine)

    return [
        _view(info, rows.get(info.id), config)
        for info in sorted(discover_providers(config.provider_dir), key=lambda i: i.id)
    ]


@router.post("/providers/{id}/enable", response_model=ProviderView)
async def enable_provider(request: Request, id: str) -> ProviderView:
    """Enable a provider and make it due immediately.

    This is the moment a fresh install stops being silent, so it is deliberately an explicit
    operator action with no default-on path anywhere (SC-013).

    Failure modes: 404 problem+json if no such provider is installed; 422 if its configuration is
    invalid, in which case it is marked ``misconfigured`` rather than enabled — a broken provider
    never blocks the service or another provider (FR-025).
    """
    return await _set_enabled(request, id, enabled=True)


@router.post("/providers/{id}/disable", response_model=ProviderView)
async def disable_provider(request: Request, id: str) -> ProviderView:
    """Disable a provider. It keeps its archive and its cursor; it simply stops being due.

    Nothing is deleted — disabling is not a way to lose history (FR-024).
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
    _require_installed(id, config.provider_dir)
    return load_provider(id, config.provider_dir).config_model.model_json_schema()


@router.put("/providers/{id}/config", response_model=ProviderView)
async def update_provider_config(
    request: Request,
    id: str,
    body: Annotated[dict[str, Any], Body()],
) -> ProviderView:
    """Validate and persist one provider's configuration without enabling it.

    The opaque settings stay in the local database and are deliberately never included in a
    response: provider schemas can contain credentials. The scheduler reads this row for its next
    run, so this endpoint is a real configuration write rather than UI-only state.

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
    try:
        load_provider(id, config.provider_dir).config_model.model_validate(body)
    except ValidationError as exc:
        raise ProblemError(
            status=422,
            title="Provider configuration is invalid",
            detail="; ".join(error["msg"] for error in exc.errors()),
            type=error_type("provider-config-invalid"),
        ) from exc

    now = datetime.now(UTC)
    async with transaction(engine) as conn:
        exists = (await conn.execute(select(providers.c.id).where(providers.c.id == id))).first()
        if exists is None:
            # Saving settings must not start network activity. Enable is a separate, deliberate
            # operation, preserving the fresh-install silence guarantee (SC-013).
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

    rows = await _provider_rows(engine)
    return _view(info, rows.get(id), config)


@router.post("/providers/{id}/sync", status_code=202, response_model=SyncQueued)
async def sync_now(
    request: Request,
    id: str,
    # Annotated rather than `= Body(...)`: a call in a default is ruff's B008, and this is
    # FastAPI's own current recommendation anyway.
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> SyncQueued:
    """Queue a run now by making the provider due.

    The API cannot spawn a run itself — it may not import ``aggregato.sync`` — and does not need to:
    setting ``next_run_at`` to now puts the provider at the front of the due queue and the scheduler
    dispatches it within a poll interval (research.md R1). Returns 202 because the run has been
    *queued*, not performed.

    Args:
        request: The request.
        id: Provider id.
        body: Optional ``{"mode": "incremental" | "full"}``. A ``full`` request clears the cursor,
            because that is what "full" means and a full run that resumed from a cursor would not be
            one.

    Returns:
        The ``lineage_id`` the run and its retries share, so a caller follows the attempts as one
        piece of work (FR-019).

    Failure modes: 404 if not installed; 409 if a run is already in flight — two concurrent runs of
    one provider would race on its cursor.
    """
    engine: AsyncEngine = request.app.state.engine
    config: Config = request.app.state.config
    _require_installed(id, config.provider_dir)
    mode = (body or {}).get("mode", "incremental")
    if mode not in {"incremental", "full"}:
        raise ProblemError(
            status=422,
            title="Unknown sync mode",
            detail=f"mode must be 'incremental' or 'full', got {mode!r}",
            type=error_type("invalid-sync-mode"),
        )

    now = datetime.now(UTC)
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

        values: dict[str, Any] = {"next_run_at": now}
        if mode == "full":
            # A "full" run that resumed from a cursor would not be full.
            values["cursor"] = None
        await conn.execute(
            update(provider_state).where(provider_state.c.provider_id == id).values(values)
        )
        # A degraded provider asked to sync gets its ladder reset: the operator has presumably fixed
        # whatever it was complaining about, and refusing to try would be unhelpful (FR-021).
        await conn.execute(
            update(providers)
            .where(providers.c.id == id)
            .values(status=str(ProviderStatus.IDLE), updated_at=now)
        )

    return SyncQueued(lineage_id=lineage)


@router.post("/providers/{id}/check", response_model=CheckResultView)
async def check_provider(request: Request, id: str) -> CheckResultView:
    """Report the provider's last credential-check result.

    Verifying credentials means executing provider code, which happens in a child process the
    scheduler owns (FR-037) — so this reports the most recent outcome rather than blocking a request
    on a third-party platform. A provider that has never run reports ``ok: false`` with no
    error class, meaning "not yet known" rather than "broken".

    ponytail: an on-demand check would need the API to ask the scheduler for one, which is a
    request/response channel between two processes that does not exist yet. The upgrade path is a
    ``check_requested_at`` column the scheduler polls; not built until the UI proves it needs the
    immediacy.
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
        return CheckResultView(ok=False, detail="this provider has not run yet")
    if row.error_class is None:
        return CheckResultView(ok=True, detail="last run completed without an error")
    return CheckResultView(
        ok=False,
        error_class=ErrorClass(row.error_class),
        detail=row.error_message,
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
        row = (await conn.execute(select(providers.c.enabled).where(providers.c.id == id))).first()
    if row is None or not row.enabled:
        raise ProblemError(
            status=409,
            title="Provider is not enabled",
            detail=f"enable {id} before importing an export",
            type=error_type("provider-not-enabled"),
        )

    path = await _store_import(config.data_dir, id, file)
    now = datetime.now(UTC)
    lineage = uuid.uuid4()
    async with transaction(engine) as conn:
        await conn.execute(
            import_jobs.insert().values(provider_id=id, path=str(path), created_at=now)
        )
        await conn.execute(
            update(provider_state).where(provider_state.c.provider_id == id).values(next_run_at=now)
        )
        await conn.execute(
            update(providers)
            .where(providers.c.id == id)
            .values(status=str(ProviderStatus.IDLE), updated_at=now)
        )
    return SyncQueued(lineage_id=lineage)


async def _store_import(data_dir: Path, provider_id: str, upload: UploadFile) -> Path:
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
                output.write(chunk)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return target


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
    misconfigured = enabled and provider_config is not None and provider_config.error is not None
    status = (
        ProviderStatus.MISCONFIGURED
        if misconfigured
        else (ProviderStatus.IDLE if enabled else ProviderStatus.DISABLED)
    )
    now = datetime.now(UTC)

    async with transaction(engine) as conn:
        exists = (
            await conn.execute(select(providers.c.id).where(providers.c.id == provider_id))
        ).first()
        if exists is None:
            await conn.execute(
                providers.insert().values(
                    id=provider_id,
                    enabled=enabled,
                    status=str(status),
                    acquisition=info.acquisition,
                    schema_version=info.schema_version,
                    reviewed=info.reviewed,
                    config={},
                    last_error=(
                        {
                            "error_class": str(ErrorClass.INTERNAL),
                            "message": provider_config.error,
                            "action_required": "fix this provider's configuration and enable again",
                        }
                        if misconfigured and provider_config is not None
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
            await conn.execute(
                update(providers)
                .where(providers.c.id == provider_id)
                .values(enabled=enabled, status=str(status), updated_at=now)
            )
            await conn.execute(
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

    if misconfigured:
        raise ProblemError(
            status=422,
            title="Provider configuration is invalid",
            detail=(
                f"{provider_id} is marked misconfigured and was not enabled: "
                f"{provider_config.error if provider_config else 'unknown error'}"
            ),
            type=error_type("provider-misconfigured"),
        )

    rows = await _provider_rows(engine)
    return _view(info, rows.get(provider_id), config)
