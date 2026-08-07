"""Operational history: sync attempts and retained poison records ."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select, update
from starlette.requests import Request

from aggregato.api.clock import now as request_now
from aggregato.api.errors import ProblemError, error_type
from aggregato.api.pagination import clamp_limit
from aggregato.api.queries import fetch_page
from aggregato.api.schemas import PageResponse
from aggregato.db.engine import transaction
from aggregato.db.schema import ingest_failures, provider_state, providers, replay_jobs, sync_runs
from aggregato.domain.enums import ErrorClass, IngestStage, RunStatus

router = APIRouter(tags=["operations"])


class SyncRun(BaseModel):
    id: int
    provider_id: str
    lineage_id: uuid.UUID
    attempt: int
    mode: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    items_seen: int
    items_written: int
    items_failed: int
    error_class: ErrorClass | None = None
    error_message: str | None = None
    next_retry_at: datetime | None = None
    log_excerpt: str | None = None


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


@router.get("/providers/{id}/runs", response_model=PageResponse[SyncRun])
async def provider_runs(
    request: Request,
    id: str,
    status: RunStatus | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> PageResponse[SyncRun]:
    """Return attempts newest-first; lineage and attempt make retry grouping explicit."""
    query = select(sync_runs).where(sync_runs.c.provider_id == id)
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


def _run(row: Any) -> SyncRun:
    return SyncRun(
        id=row.id,
        provider_id=row.provider_id,
        lineage_id=row.lineage_id,
        attempt=row.attempt,
        mode=row.mode,
        status=RunStatus(row.status),
        started_at=_required(_aware(row.started_at)),
        finished_at=_aware(row.finished_at),
        items_seen=row.items_seen,
        items_written=row.items_written,
        items_failed=row.items_failed,
        error_class=ErrorClass(row.error_class) if row.error_class else None,
        error_message=row.error_message,
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
