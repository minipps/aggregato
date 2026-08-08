"""Durable import and replay job leases owned by the scheduler process.

The two job tables stay separate because imports own files while replays own retained failure
payloads. Their lease lifecycle is the same, so this module keeps claiming, finishing, and failing
that lifecycle in one place and returns typed records to the run dispatcher.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from aggregato.db.engine import transaction
from aggregato.db.schema import import_jobs, ingest_failures, replay_jobs
from aggregato.domain.models import RawRecord

IMPORT_LEASE = timedelta(minutes=15)
REPLAY_LEASE = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class ImportLease:
    """An upload claimed by this worker, including the owner token for finalization."""

    job_id: int
    path: Path
    lineage_id: UUID | None
    owner: str


@dataclass(frozen=True, slots=True)
class ReplayLease:
    """A retained failure claimed for normalization replay."""

    job_id: int
    lineage_id: UUID
    record: RawRecord
    owner: str
    failure_id: int


def _lease_available(
    marker: ColumnElement[Any], expires: ColumnElement[Any], now: datetime
) -> ColumnElement[bool]:
    """Build the shared stale-or-unclaimed lease predicate for either job table."""
    return or_(
        marker.is_(None),
        expires.is_(None),
        expires <= now,
    )


async def claim_import(
    engine: AsyncEngine, provider_id: str, *, now: datetime
) -> ImportLease | None:
    """Claim the oldest unfinished upload, reclaiming a stale lease atomically."""
    owner = uuid.uuid4().hex
    async with transaction(engine) as conn:
        row = (
            await conn.execute(
                select(import_jobs)
                .where(
                    import_jobs.c.provider_id == provider_id,
                    import_jobs.c.finished_at.is_(None),
                    import_jobs.c.failed_at.is_(None),
                    _lease_available(import_jobs.c.started_at, import_jobs.c.lease_expires_at, now),
                )
                .order_by(import_jobs.c.id)
                .limit(1)
            )
        ).first()
        if row is None:
            return None

        job_id = int(row._mapping["id"])
        result = await conn.execute(
            update(import_jobs)
            .where(
                import_jobs.c.id == job_id,
                import_jobs.c.finished_at.is_(None),
                import_jobs.c.failed_at.is_(None),
                _lease_available(import_jobs.c.started_at, import_jobs.c.lease_expires_at, now),
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
        return ImportLease(
            job_id=job_id,
            path=Path(str(row._mapping["path"])),
            lineage_id=row._mapping.get("lineage_id"),
            owner=owner,
        )


async def claim_replay(
    engine: AsyncEngine, provider_id: str, *, now: datetime
) -> ReplayLease | None:
    """Claim the oldest unresolved replay payload, reclaiming a stale lease atomically."""
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
                    _lease_available(replay_jobs.c.claimed_at, replay_jobs.c.lease_expires_at, now),
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
                _lease_available(replay_jobs.c.claimed_at, replay_jobs.c.lease_expires_at, now),
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
        return ReplayLease(
            job_id=job_id,
            lineage_id=row.lineage_id,
            record=RawRecord(native_id=str(row.native_id), payload=dict(row.raw_payload)),
            owner=owner,
            failure_id=int(row.failure_id),
        )


async def _finish_job(
    engine: AsyncEngine,
    table: Table,
    job_id: int,
    *,
    owner: str,
    now: datetime,
    failure_id: int | None = None,
) -> bool:
    """Finish either job kind only when this worker still owns its lease."""
    async with transaction(engine) as conn:
        result = await conn.execute(
            update(table)
            .where(
                table.c.id == job_id,
                table.c.finished_at.is_(None),
                table.c.lease_owner == owner,
            )
            .values(finished_at=now, lease_owner=None, lease_expires_at=None)
        )
        if result.rowcount == 1 and failure_id is not None:
            await conn.execute(
                update(ingest_failures)
                .where(
                    ingest_failures.c.id == failure_id,
                    ingest_failures.c.resolved_at.is_(None),
                )
                .values(resolved_at=now)
            )
        return result.rowcount == 1


async def finish_import(engine: AsyncEngine, lease: ImportLease, *, now: datetime) -> bool:
    """Mark an upload complete if its lease is still owned by this worker."""
    return await _finish_job(engine, import_jobs, lease.job_id, owner=lease.owner, now=now)


async def finish_replay(engine: AsyncEngine, lease: ReplayLease, *, now: datetime) -> bool:
    """Mark a replay complete and resolve its source failure in one transaction."""
    return await _finish_job(
        engine,
        replay_jobs,
        lease.job_id,
        owner=lease.owner,
        now=now,
        failure_id=lease.failure_id,
    )


async def _fail_job(
    engine: AsyncEngine,
    table: Table,
    job_id: int,
    *,
    owner: str,
    values: dict[str, object],
) -> None:
    """Fail either job kind only when this worker still owns its lease."""
    async with transaction(engine) as conn:
        await conn.execute(
            update(table)
            .where(
                table.c.id == job_id,
                table.c.lease_owner == owner,
            )
            .values(**values, lease_owner=None, lease_expires_at=None)
        )


async def fail_import(
    engine: AsyncEngine, lease: ImportLease, *, error: BaseException, now: datetime
) -> None:
    """Mark a failed upload and release its lease."""
    await _fail_job(
        engine,
        import_jobs,
        lease.job_id,
        owner=lease.owner,
        values={"failed_at": now, "error_class": type(error).__name__, "error": str(error)[:1000]},
    )


async def fail_replay(
    engine: AsyncEngine, lease: ReplayLease, *, error: BaseException, now: datetime
) -> None:
    """Mark a failed replay and release its lease."""
    await _fail_job(
        engine,
        replay_jobs,
        lease.job_id,
        owner=lease.owner,
        values={"failed_at": now, "error": type(error).__name__ + ": " + str(error)[:1000]},
    )
