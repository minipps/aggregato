"""Poison-record capture: one bad record never blocks a run .

A single unparseable record in a ten-year history is the normal case, not the exceptional one — a
platform changes a field, an old row predates a convention, a title contains something nobody
expected. If that stops the run, the operator gets nothing and no diagnosis. So the record goes here
with **its payload**, the run continues, and ``items_failed`` counts it.

Storing the payload is the part that matters. Without it, "one record failed to convert" is an
unreproducible bug report; with it, a fixed provider replays the stored payload and the entry
appears without re-syncing the platform (, the same replay principle as research.md ).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aggregato.db.schema import ingest_failures
from aggregato.domain.enums import IngestStage
from aggregato.domain.models import NormalizedBatch, RawRecord


@dataclass(frozen=True)
class CapturedFailure:
    """One record that did not convert, and enough context to file a plugin bug."""

    id: int
    provider_id: str
    sync_run_id: int
    stage: IngestStage
    error: str
    raw_payload: dict[str, Any]


async def capture_failure(
    conn: AsyncConnection,
    *,
    provider_id: str,
    sync_run_id: int,
    stage: IngestStage,
    error: BaseException | str,
    raw_payload: dict[str, Any],
    now: datetime,
) -> int:
    """Record one failed record and return its id.

    Args:
        conn: Connection inside the writer's transaction.
        provider_id: Which provider produced the record.
        sync_run_id: The run it arrived in, so a burst of failures is attributable to one sync.
        stage: Where it died — ``fetch``, ``validate``, ``normalize``, or ``write``. This is what
            lets an operator tell a plugin bug from a host bug without reading a traceback.
        error: The exception or an explanatory message. Exceptions are rendered as
            ``ClassName: message``, because the class alone rarely says enough and the traceback is
            too much for a list view (it goes to the run's ``log_excerpt`` instead).
        raw_payload: The record verbatim. This is the replay source; a failure stored without it is
            barely worth storing.
        now: From the injected clock.

    Returns:
        The new row's id.
    """
    result = await conn.execute(
        ingest_failures.insert().values(
            provider_id=provider_id,
            sync_run_id=sync_run_id,
            raw_payload=raw_payload,
            error=_render(error),
            stage=str(stage),
            created_at=now,
        )
    )
    # inserted_primary_key is Optional in the general case (an INSERT..SELECT has none);
    # a single-row VALUES insert on a table with a surrogate key always has one.
    primary_key = result.inserted_primary_key
    assert primary_key is not None
    return int(primary_key[0])


def _render(error: BaseException | str) -> str:
    if isinstance(error, str):
        return error
    message = str(error)
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


async def unresolved_failures(
    conn: AsyncConnection,
    *,
    provider_id: str | None = None,
    limit: int = 100,
) -> Sequence[CapturedFailure]:
    """The failures still awaiting a fixed provider, newest first.

    Args:
        conn: Any connection.
        provider_id: Restrict to one provider, or ``None`` for all.
        limit: Maximum rows.

    Returns:
        Captured failures, newest first.
    """
    query = select(
        ingest_failures.c.id,
        ingest_failures.c.provider_id,
        ingest_failures.c.sync_run_id,
        ingest_failures.c.stage,
        ingest_failures.c.error,
        ingest_failures.c.raw_payload,
    ).where(ingest_failures.c.resolved_at.is_(None))
    if provider_id is not None:
        query = query.where(ingest_failures.c.provider_id == provider_id)
    query = query.order_by(ingest_failures.c.created_at.desc(), ingest_failures.c.id.desc())
    result = await conn.execute(query.limit(limit))
    return [
        CapturedFailure(
            id=row.id,
            provider_id=row.provider_id,
            sync_run_id=row.sync_run_id,
            stage=IngestStage(row.stage),
            error=row.error,
            raw_payload=row.raw_payload,
        )
        for row in result
    ]


async def mark_resolved(conn: AsyncConnection, failure_ids: Sequence[int], *, now: datetime) -> int:
    """Mark failures as resolved after a successful replay.

    Rows are kept rather than deleted: the retention job decides when they age out, and it keeps
    failures longer than successes by default because a failure is the thing someone will want to
    look at later .

    Args:
        conn: Connection inside the replay's transaction.
        failure_ids: The rows a replay converted successfully.
        now: From the injected clock.

    Returns:
        How many rows were updated.
    """
    if not failure_ids:
        return 0
    result = await conn.execute(
        update(ingest_failures).where(ingest_failures.c.id.in_(failure_ids)).values(resolved_at=now)
    )
    return result.rowcount


async def replay_failure(
    conn: AsyncConnection,
    *,
    failure: CapturedFailure,
    normalize: object,
    write: object,
    now: datetime,
) -> bool:
    """Re-run a stored payload through a corrected normalizer and mark it resolved on success.

    ``normalize`` and ``write`` are intentionally narrow callables supplied by the host. Keeping
    this module free of provider imports means replay remains the same transactionally safe storage
    operation for every integration.
    """
    from collections.abc import Awaitable, Callable
    from typing import cast

    normalizer = cast(Callable[[RawRecord], NormalizedBatch], normalize)
    writer = cast(Callable[[RawRecord, NormalizedBatch], Awaitable[None]], write)
    payload = dict(failure.raw_payload)
    native_id = str(payload.get("id", failure.id))
    try:
        batch = normalizer(RawRecord(native_id=native_id, payload=payload))
        await writer(RawRecord(native_id=native_id, payload=payload), batch)
    except (ValueError, TypeError):
        return False
    await mark_resolved(conn, [failure.id], now=now)
    return True
