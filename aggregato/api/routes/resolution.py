"""The operator's open identity-resolution queue ."""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import select, update
from starlette.requests import Request

from aggregato.api.errors import ProblemError
from aggregato.api.pagination import clamp_limit
from aggregato.api.queries import fetch_page
from aggregato.api.routes.identity import MergeLogEntry, _entry
from aggregato.api.schemas import PageResponse
from aggregato.db.engine import transaction
from aggregato.db.schema import merge_log, provider_items, resolution_queue
from aggregato.domain.clock import SYSTEM_CLOCK
from aggregato.domain.enums import ResolutionDecision, ResolutionSubject
from aggregato.ingest.merge import merge_creators, merge_works

router = APIRouter(tags=["identity"])


class ResolutionItem(BaseModel):
    id: int
    subject: ResolutionSubject
    provider_id: str
    payload_ref: int | None
    candidates: list[dict[str, Any]]
    proposed: dict[str, Any]
    suggestion_kind: str | None
    created_at: Any


class DecisionRequest(BaseModel):
    decision: ResolutionDecision
    target_id: uuid.UUID | None = None


@router.get("/resolution-queue", response_model=PageResponse[ResolutionItem])
async def list_resolution_queue(
    request: Request,
    subject: ResolutionSubject | None = None,
    suggestion_kind: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> PageResponse[ResolutionItem]:
    query = select(resolution_queue).where(resolution_queue.c.decided_at.is_(None))
    if subject is not None:
        query = query.where(resolution_queue.c.subject == str(subject))
    if suggestion_kind is not None:
        query = query.where(resolution_queue.c.suggestion_kind == suggestion_kind)
    async with transaction(request.app.state.engine) as conn:
        page = await fetch_page(
            conn,
            query,
            sort_col=resolution_queue.c.created_at,
            id_col=resolution_queue.c.id,
            sort="created_at",
            order="asc",
            cursor=cursor,
            limit=clamp_limit(limit),
        )
    return PageResponse(items=[_item(row) for row in page.items], next_cursor=page.next_cursor)


def _item(row: Any) -> ResolutionItem:
    return ResolutionItem(
        id=row.id,
        subject=ResolutionSubject(row.subject),
        provider_id=row.provider_id,
        payload_ref=row.payload_ref,
        candidates=row.candidates,
        proposed=row.proposed,
        suggestion_kind=row.suggestion_kind,
        created_at=row.created_at,
    )


@router.post("/resolution-queue/{id}/decide", response_model=MergeLogEntry)
async def decide(request: Request, id: int, body: DecisionRequest) -> MergeLogEntry:
    """Apply a durable manual decision; linked provider items stay linked after resync."""
    if body.decision == ResolutionDecision.LINKED and body.target_id is None:
        raise ProblemError(409, "Conflict", "target_id is required for a linked decision.")
    if body.decision != ResolutionDecision.LINKED and body.target_id is not None:
        raise ProblemError(409, "Conflict", "target_id is only valid for a linked decision.")
    async with transaction(request.app.state.engine) as conn:
        item = (
            await conn.execute(select(resolution_queue).where(resolution_queue.c.id == id))
        ).first()
        if item is None:
            raise ProblemError(404, "Not Found", f"No resolution item with id {id}.")
        if item.decided_at is not None:
            raise ProblemError(409, "Conflict", "This resolution item was already decided.")
        now = SYSTEM_CLOCK.now()
        # Merge-log snapshots live in a JSON column.  A queue row includes ``created_at`` (and may
        # contain UUID-valued payload data), so retain it through FastAPI's JSON-safe encoder just
        # as the merge service does for its own snapshots.
        queue_snapshot = {"resolution_queue": [jsonable_encoder(dict(item._mapping))]}
        try:
            source_id = await _live_identity(conn, item.subject, await _source_id(conn, item))
            if body.decision == ResolutionDecision.LINKED:
                assert body.target_id is not None
                target_id = await _live_identity(conn, item.subject, body.target_id)
                log = (
                    await (
                        merge_works(conn, winner_id=target_id, loser_ids=[source_id], now=now)
                        if item.subject == str(ResolutionSubject.WORK)
                        else merge_creators(
                            conn, winner_id=target_id, loser_ids=[source_id], now=now
                        )
                    )
                    if target_id != source_id
                    else await _queue_decision_log(
                        conn, item.subject, source_id, queue_snapshot, now
                    )
                )
            else:
                log = await _queue_decision_log(
                    conn,
                    item.subject,
                    source_id,
                    queue_snapshot,
                    now,
                    operation="split" if body.decision == ResolutionDecision.SPLIT else "merge",
                )
        except (LookupError, ValueError) as exc:
            raise ProblemError(409, "Conflict", str(exc)) from exc
        snapshot = dict(log.snapshot)
        snapshot.update(queue_snapshot)
        await conn.execute(
            update(merge_log).where(merge_log.c.id == log.id).values(snapshot=snapshot)
        )
        await conn.execute(
            update(resolution_queue)
            .where(resolution_queue.c.id == id)
            .values(decided_at=now, decision=str(body.decision))
        )
        # Re-read because JSON snapshot was amended above.
        log = (await conn.execute(select(merge_log).where(merge_log.c.id == log.id))).one()
        return _entry(log)


async def _source_id(conn: Any, item: Any) -> uuid.UUID:
    if item.subject == str(ResolutionSubject.CREATOR):
        value = item.proposed.get("creator_id") if isinstance(item.proposed, dict) else None
        if value is None:
            raise ValueError("resolution item has no proposed creator")
        return uuid.UUID(str(value))
    if item.payload_ref is None:
        raise ValueError("resolution item has no provider item")
    value = (
        await conn.execute(
            select(provider_items.c.work_id).where(provider_items.c.id == item.payload_ref)
        )
    ).scalar_one_or_none()
    if value is None:
        raise ValueError("resolution item has no proposed work")
    return cast(uuid.UUID, value)


async def _live_identity(conn: Any, subject: str, identity_id: uuid.UUID) -> uuid.UUID:
    """Follow active merge-log successors so queued historical candidates remain actionable."""
    rows = await conn.execute(
        select(merge_log.c.winner_id, merge_log.c.loser_ids).where(
            merge_log.c.subject == subject,
            merge_log.c.operation == "merge",
            merge_log.c.undone_at.is_(None),
        )
    )
    successors = {
        uuid.UUID(str(loser_id)): row.winner_id for row in rows for loser_id in row.loser_ids
    }
    seen: set[uuid.UUID] = set()
    while identity_id in successors and identity_id not in seen:
        seen.add(identity_id)
        identity_id = successors[identity_id]
    return identity_id


async def _queue_decision_log(
    conn: Any,
    subject: str,
    source_id: uuid.UUID,
    snapshot: dict[str, Any],
    now: Any,
    *,
    operation: str = "merge",
) -> Any:
    """Record a queue-only decision, including an already-satisfied historical link."""
    return (
        await conn.execute(
            merge_log.insert()
            .values(
                subject=subject,
                operation=operation,
                winner_id=source_id,
                loser_ids=[],
                moved_credit_ids=None,
                performed_at=now,
                snapshot=snapshot,
            )
            .returning(merge_log)
        )
    ).one()
