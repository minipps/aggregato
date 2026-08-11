"""Operational history: sync attempts and retained poison records ."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

from aggregato.api.clock import now as request_now
from aggregato.api.deps import require_websocket_auth
from aggregato.api.errors import ProblemError, error_type
from aggregato.api.pagination import clamp_limit
from aggregato.api.queries import fetch_page
from aggregato.api.schemas import PageResponse
from aggregato.db.engine import transaction
from aggregato.db.schema import ingest_failures, provider_state, providers, replay_jobs, sync_runs
from aggregato.domain.enums import (
    ErrorClass,
    FetchMode,
    IngestStage,
    ProviderStatus,
    RunPhase,
    RunStatus,
)

router = APIRouter(tags=["operations"])


class SyncRun(BaseModel):
    id: int
    provider_id: str
    lineage_id: uuid.UUID
    attempt: int
    mode: str
    status: RunStatus
    phase: RunPhase
    started_at: datetime
    finished_at: datetime | None = None
    updated_at: datetime
    items_seen: int
    items_written: int
    items_failed: int
    progress_total: int | None = None
    progress_percent: int | None = None
    checkpoint_count: int
    last_checkpoint_at: datetime | None = None
    cursor_before: dict[str, object] | None = None
    cursor_after: dict[str, object] | None = None
    error_class: ErrorClass | None = None
    error_message: str | None = None
    next_retry_at: datetime | None = None
    log_excerpt: str | None = None


class SyncProviderState(BaseModel):
    """The scheduler-facing state needed to distinguish queued from running work."""

    id: str
    enabled: bool
    status: ProviderStatus
    requested_mode: FetchMode | None = None
    requested_lineage_id: uuid.UUID | None = None
    next_run_at: datetime | None = None


class SyncSnapshot(BaseModel):
    """The latest durable sync state, shared by HTTP and websocket consumers."""

    type: Literal["snapshot"] = "snapshot"
    generated_at: datetime
    providers: list[SyncProviderState]
    runs: list[SyncRun]


class IngestFailure(BaseModel):
    id: int
    provider_id: str
    sync_run_id: int
    native_id: str | None = None
    stage: IngestStage
    error: str
    raw_payload: dict[str, object]
    created_at: datetime
    resolved_at: datetime | None = None


class ReplayResult(BaseModel):
    replayed: bool = False
    queued: bool = False
    job_id: int | None = None


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo is not None else value.replace(tzinfo=UTC)


def _required(value: datetime | None) -> datetime:
    assert value is not None
    return value


@router.get("/sync/status", response_model=SyncSnapshot)
async def sync_status(
    request: Request,
    provider_id: str | None = None,
    lineage_id: uuid.UUID | None = None,
) -> SyncSnapshot:
    """Return the current queue and latest non-diagnostic run for each provider."""
    return await _snapshot(
        request.app.state.engine,
        generated_at=request_now(request),
        provider_id=provider_id,
        lineage_id=lineage_id,
    )


@router.websocket("/ws/sync")
async def sync_socket(
    websocket: WebSocket,
    provider_id: str | None = None,
    lineage_id: uuid.UUID | None = None,
) -> None:
    """Stream database-backed sync snapshots to a browser or another authenticated client.

    The API and worker are separate processes, so this intentionally polls the durable row rather
    than keeping an in-memory event bus.  A reconnect always receives a complete current snapshot,
    and a worker restart cannot strand a websocket in an invented state.
    """
    try:
        await require_websocket_auth(websocket)
    except ProblemError:
        # WebSocket routes do not pass through the HTTP problem-detail handlers.  A policy close is
        # the interoperable way to reject an unauthenticated handshake without putting credentials
        # in a URL or sending a JSON error before the connection is accepted.
        await websocket.close(code=1008, reason="authentication required")
        return

    await websocket.accept()
    previous_state: str | None = None
    try:
        while True:
            snapshot = await _snapshot(
                websocket.app.state.engine,
                generated_at=websocket.app.state.clock.now(),
                provider_id=provider_id,
                lineage_id=lineage_id,
            )
            state = json.dumps(
                snapshot.model_dump(mode="json", exclude={"generated_at"}),
                separators=(",", ":"),
                sort_keys=True,
            )
            if state != previous_state:
                payload = json.dumps(
                    snapshot.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
                )
                await websocket.send_text(payload)
                previous_state = state
            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        return


@router.get("/providers/{id}/runs", response_model=PageResponse[SyncRun])
async def provider_runs(
    request: Request,
    id: str,
    status: RunStatus | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> PageResponse[SyncRun]:
    """Return attempts newest-first; lineage and attempt make retry grouping explicit."""
    query = select(sync_runs).where(
        sync_runs.c.provider_id == id,
        sync_runs.c.mode != str(FetchMode.CHECK),
    )
    if status is not None:
        query = query.where(sync_runs.c.status == str(status))
    async with transaction(request.app.state.engine) as conn:
        page = await fetch_page(
            conn,
            query,
            sort_col=sync_runs.c.started_at,
            id_col=sync_runs.c.id,
            sort="started_at",
            order="desc",
            cursor=cursor,
            limit=clamp_limit(limit),
        )
    return PageResponse(items=[_run(row) for row in page.items], next_cursor=page.next_cursor)


@router.get("/ingest-failures", response_model=PageResponse[IngestFailure])
async def list_ingest_failures(
    request: Request,
    provider: str | None = None,
    resolved: bool = False,
    limit: int | None = None,
    cursor: str | None = None,
) -> PageResponse[IngestFailure]:
    """List retained failed records, including their replay payloads."""
    query = select(ingest_failures).where(
        ingest_failures.c.resolved_at.is_not(None)
        if resolved
        else ingest_failures.c.resolved_at.is_(None)
    )
    if provider is not None:
        query = query.where(ingest_failures.c.provider_id == provider)
    async with transaction(request.app.state.engine) as conn:
        page = await fetch_page(
            conn,
            query,
            sort_col=ingest_failures.c.created_at,
            id_col=ingest_failures.c.id,
            sort="created_at",
            order="desc",
            cursor=cursor,
            limit=clamp_limit(limit),
        )
    return PageResponse(items=[_failure(row) for row in page.items], next_cursor=page.next_cursor)


@router.post("/ingest-failures/{id}/replay", status_code=202, response_model=ReplayResult)
async def replay_ingest_failure(request: Request, id: int) -> ReplayResult:
    """Queue a replay; normalization and writing stay in the worker child/parent boundary."""
    now = request_now(request)
    async with transaction(request.app.state.engine) as conn:
        row = (
            await conn.execute(
                select(ingest_failures.c.provider_id, ingest_failures.c.native_id).where(
                    ingest_failures.c.id == id,
                    ingest_failures.c.resolved_at.is_(None),
                )
            )
        ).first()
        if row is None:
            return ReplayResult()
        if row.native_id is None:
            raise ProblemError(
                status=409,
                title="Failure has no replay identity",
                detail=(
                    "legacy failures without a captured provider identity cannot be replayed safely"
                ),
                type=error_type("replay-identity-missing"),
            )

        active = (
            await conn.execute(
                select(providers.c.enabled, providers.c.status).where(
                    providers.c.id == row.provider_id
                )
            )
        ).first()
        if active is None or not active.enabled:
            raise ProblemError(
                status=409,
                title="Provider is not enabled",
                detail=f"enable {row.provider_id} before replaying a failure",
                type=error_type("provider-not-enabled"),
            )
        if active.status == "syncing":
            raise ProblemError(
                status=409,
                title="A sync is already running",
                detail=f"{row.provider_id} is already syncing; replay will be retried later",
                type=error_type("sync-in-flight"),
            )

        existing = (
            await conn.execute(
                select(replay_jobs.c.id)
                .where(
                    replay_jobs.c.failure_id == id,
                    replay_jobs.c.finished_at.is_(None),
                    replay_jobs.c.failed_at.is_(None),
                )
                .order_by(replay_jobs.c.id.desc())
                .limit(1)
            )
        ).first()
        if existing is not None:
            return ReplayResult(queued=True, job_id=int(existing.id))

        lineage = uuid.uuid4()
        result = await conn.execute(
            replay_jobs.insert().values(
                failure_id=id,
                provider_id=row.provider_id,
                lineage_id=lineage,
                created_at=now,
            )
        )
        primary_key = result.inserted_primary_key
        assert primary_key is not None
        state_update = await conn.execute(
            update(provider_state)
            .where(provider_state.c.provider_id == row.provider_id)
            .values(next_run_at=now)
        )
        if state_update.rowcount != 1:
            raise ProblemError(
                status=503,
                title="Provider state is incomplete",
                detail="the provider has no scheduling state; repair it before queueing replay",
                type=error_type("provider-state-missing"),
            )
    return ReplayResult(queued=True, job_id=int(primary_key[0]))


async def _snapshot(
    engine: AsyncEngine,
    *,
    generated_at: datetime,
    provider_id: str | None,
    lineage_id: uuid.UUID | None,
) -> SyncSnapshot:
    """Read a complete sync snapshot in one transaction for HTTP and websocket callers."""
    provider_query = select(
        providers.c.id,
        providers.c.enabled,
        providers.c.status,
        provider_state.c.requested_mode,
        provider_state.c.requested_lineage_id,
        provider_state.c.next_run_at,
    ).select_from(
        providers.outerjoin(provider_state, provider_state.c.provider_id == providers.c.id)
    )
    latest_query = select(func.max(sync_runs.c.id).label("id")).where(
        sync_runs.c.mode != str(FetchMode.CHECK)
    )
    if provider_id is not None:
        provider_query = provider_query.where(providers.c.id == provider_id)
        latest_query = latest_query.where(sync_runs.c.provider_id == provider_id)
    if lineage_id is not None:
        latest_query = latest_query.where(sync_runs.c.lineage_id == lineage_id)
    latest_ids = latest_query.group_by(sync_runs.c.provider_id).subquery()
    run_query = select(sync_runs).where(sync_runs.c.id.in_(select(latest_ids.c.id)))
    if provider_id is not None:
        run_query = run_query.where(sync_runs.c.provider_id == provider_id)
    if lineage_id is not None:
        run_query = run_query.where(sync_runs.c.lineage_id == lineage_id)
    run_query = run_query.order_by(sync_runs.c.started_at.desc(), sync_runs.c.id.desc())

    async with transaction(engine) as conn:
        provider_rows = list(await conn.execute(provider_query))
        run_rows = list(await conn.execute(run_query))
    return SyncSnapshot(
        generated_at=_required(_aware(generated_at)),
        providers=[
            SyncProviderState(
                id=row.id,
                enabled=bool(row.enabled),
                status=ProviderStatus(row.status),
                requested_mode=row.requested_mode,
                requested_lineage_id=row.requested_lineage_id,
                next_run_at=_aware(row.next_run_at),
            )
            for row in provider_rows
        ],
        runs=[_run(row) for row in run_rows],
    )


def _run(row: Any) -> SyncRun:
    total = getattr(row, "progress_total", None)
    seen = int(row.items_seen or 0)
    percent = None if total is None else 100 if total == 0 else min(100, round(seen * 100 / total))
    updated_at = _aware(getattr(row, "updated_at", None))
    if updated_at is None:
        updated_at = _required(_aware(row.started_at))
    return SyncRun(
        id=row.id,
        provider_id=row.provider_id,
        lineage_id=row.lineage_id,
        attempt=row.attempt,
        mode=row.mode,
        status=RunStatus(row.status),
        phase=RunPhase(getattr(row, "phase", RunPhase.STARTING)),
        started_at=_required(_aware(row.started_at)),
        finished_at=_aware(row.finished_at),
        updated_at=updated_at,
        items_seen=seen,
        items_written=row.items_written,
        items_failed=row.items_failed,
        progress_total=total,
        progress_percent=percent,
        checkpoint_count=int(getattr(row, "checkpoint_count", 0) or 0),
        last_checkpoint_at=_aware(getattr(row, "last_checkpoint_at", None)),
        cursor_before=getattr(row, "cursor_before", None),
        cursor_after=getattr(row, "cursor_after", None),
        error_class=ErrorClass(row.error_class) if row.error_class else None,
        error_message=row.error_message,
        next_retry_at=_aware(getattr(row, "next_retry_at", None)),
        log_excerpt=row.log_excerpt,
    )


def _failure(row: Any) -> IngestFailure:
    return IngestFailure(
        id=row.id,
        provider_id=row.provider_id,
        sync_run_id=row.sync_run_id,
        native_id=row.native_id,
        stage=IngestStage(row.stage),
        error=row.error,
        raw_payload=row.raw_payload,
        created_at=_required(_aware(row.created_at)),
        resolved_at=_aware(row.resolved_at),
    )
