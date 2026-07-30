"""Operational history: sync attempts and retained poison records (T093)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from starlette.requests import Request

from aggregato.api.pagination import clamp_limit
from aggregato.api.queries import fetch_page
from aggregato.api.schemas import PageResponse
from aggregato.config import Config
from aggregato.db.engine import transaction
from aggregato.db.schema import ingest_failures, sync_runs
from aggregato.domain.enums import ErrorClass, IngestStage, RunStatus
from aggregato.ingest.failures import CapturedFailure, replay_failure
from aggregato.ingest.writer import WriteContext, ensure_rating_scales, write_batches
from aggregato.providers.registry import load_provider

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
    stage: IngestStage
    error: str
    raw_payload: dict[str, object]
    created_at: datetime
    resolved_at: datetime | None = None


class ReplayResult(BaseModel):
    replayed: bool


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
            sort="created_at",
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


@router.post("/ingest-failures/{id}/replay", response_model=ReplayResult)
async def replay_ingest_failure(request: Request, id: int) -> ReplayResult:
    """Reprocess one retained payload after its provider's normalizer has been fixed."""
    now = datetime.now(UTC)
    async with transaction(request.app.state.engine) as conn:
        row = (
            await conn.execute(
                select(ingest_failures).where(
                    ingest_failures.c.id == id, ingest_failures.c.resolved_at.is_(None)
                )
            )
        ).first()
        if row is None:
            return ReplayResult(replayed=False)
        failure = CapturedFailure(
            id=row.id,
            provider_id=row.provider_id,
            sync_run_id=row.sync_run_id,
            stage=IngestStage(row.stage),
            error=row.error,
            raw_payload=row.raw_payload,
        )
        config: Config = request.app.state.config
        provider = load_provider(failure.provider_id, config.provider_dir)
        scales = list(getattr(provider, "rating_scales", []))
        await ensure_rating_scales(conn, scales)

        async def write(raw: object, batch: object) -> None:
            counts = await write_batches(
                conn,
                WriteContext(
                    provider_id=failure.provider_id,
                    sync_run_id=failure.sync_run_id,
                    schema_version=int(getattr(provider, "schema_version", 1)),
                    now=now,
                    rating_scales={scale.id: scale for scale in scales},
                ),
                [(raw, batch)],  # type: ignore[list-item]
            )
            if counts.failed:
                raise ValueError("replayed payload still fails host validation")

        replayed = await replay_failure(
            conn,
            failure=failure,
            normalize=provider.normalize,
            write=write,
            now=now,
        )
    return ReplayResult(replayed=replayed)


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
        stage=IngestStage(row.stage),
        error=row.error,
        raw_payload=row.raw_payload,
        created_at=_required(_aware(row.created_at)),
        resolved_at=_aware(row.resolved_at),
    )
