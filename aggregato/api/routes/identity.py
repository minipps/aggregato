"""Manual identity correction and reversible merge-log operations (US4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from starlette.requests import Request

from aggregato.api.errors import ProblemError
from aggregato.db.engine import transaction
from aggregato.db.schema import (
    creator_aliases,
    creator_external_ids,
    creators,
    entries,
    external_ids,
    merge_log,
    opinions,
    provider_items,
    resolution_queue,
    work_credits,
    works,
)
from aggregato.domain.clock import SYSTEM_CLOCK
from aggregato.ingest.merge import merge_creators, merge_works
from aggregato.ingest.split import split_creator

router = APIRouter(tags=["identity"])


class MergeRequest(BaseModel):
    loser_ids: list[uuid.UUID] = Field(min_length=1)


class SplitRequest(BaseModel):
    credit_ids: list[int] = Field(min_length=1)
    new_name: str | None = None


class MergeLogEntry(BaseModel):
    id: int
    subject: str
    operation: str
    winner_id: uuid.UUID
    loser_ids: list[str]
    performed_at: datetime
    undo_url: str


def _entry(row: Any) -> MergeLogEntry:
    return MergeLogEntry(
        id=row.id,
        subject=row.subject,
        operation=row.operation,
        winner_id=row.winner_id,
        loser_ids=[str(item) for item in row.loser_ids],
        performed_at=row.performed_at,
        undo_url=f"/api/v1/merge-log/{row.id}/undo",
    )


@router.post("/works/{id}/merge", response_model=MergeLogEntry)
async def merge_work(request: Request, id: uuid.UUID, body: MergeRequest) -> MergeLogEntry:
    try:
        async with transaction(request.app.state.engine) as conn:
            row = await merge_works(
                conn, winner_id=id, loser_ids=body.loser_ids, now=SYSTEM_CLOCK.now()
            )
            return _entry(row)
    except (ValueError, LookupError) as exc:
        raise ProblemError(409, "Conflict", str(exc)) from exc


@router.post("/creators/{id}/merge", response_model=MergeLogEntry)
async def merge_creator(request: Request, id: uuid.UUID, body: MergeRequest) -> MergeLogEntry:
    try:
        async with transaction(request.app.state.engine) as conn:
            row = await merge_creators(
                conn, winner_id=id, loser_ids=body.loser_ids, now=SYSTEM_CLOCK.now()
            )
            return _entry(row)
    except (ValueError, LookupError) as exc:
        raise ProblemError(409, "Conflict", str(exc)) from exc


@router.post("/creators/{id}/split", response_model=MergeLogEntry)
async def split_creator_route(request: Request, id: uuid.UUID, body: SplitRequest) -> MergeLogEntry:
    try:
        async with transaction(request.app.state.engine) as conn:
            row = await split_creator(
                conn,
                creator_id=id,
                credit_ids=body.credit_ids,
                new_name=body.new_name,
                now=SYSTEM_CLOCK.now(),
            )
            return _entry(row)
    except (ValueError, LookupError) as exc:
        raise ProblemError(409, "Conflict", str(exc)) from exc


@router.post("/merge-log/{id}/undo")
async def undo(request: Request, id: int) -> dict[str, bool]:
    """Restore the exact pre-operation snapshot once; repeated undo is a conflict."""
    async with transaction(request.app.state.engine) as conn:
        row = (await conn.execute(select(merge_log).where(merge_log.c.id == id))).first()
        if row is None:
            raise ProblemError(404, "Not Found", f"No merge-log entry with id {id}.")
        if row.undone_at is not None:
            raise ProblemError(409, "Conflict", "This operation has already been undone.")
        try:
            await _restore(conn, row)
        except Exception as exc:
            raise ProblemError(
                409, "Conflict", "This operation can no longer be safely undone."
            ) from exc
        await conn.execute(
            update(merge_log).where(merge_log.c.id == id).values(undone_at=SYSTEM_CLOCK.now())
        )
    return {"undone": True}


async def _restore(conn: Any, log: Any) -> None:
    snapshot = log.snapshot
    # An ignored/created queue decision only changes the queue row.  It still gets a merge-log
    # entry so every decision has the same undo affordance, but it must not be interpreted as a
    # structural creator split merely because the compact log vocabulary is merge|split.
    if set(snapshot) == {"resolution_queue"}:
        for queue in snapshot["resolution_queue"]:
            await conn.execute(
                update(resolution_queue)
                .where(resolution_queue.c.id == queue["id"])
                .values(**_typed(queue))
            )
        return
    if log.operation == "split":
        moved = [int(item) for item in log.moved_credit_ids or []]
        await conn.execute(delete(work_credits).where(work_credits.c.id.in_(moved)))
        await conn.execute(delete(creators).where(creators.c.id == log.winner_id))
        await _insert(conn, work_credits, snapshot["work_credits"])
    elif log.subject == "creator":
        ids = [log.winner_id, *(uuid.UUID(item) for item in log.loser_ids)]
        await conn.execute(delete(work_credits).where(work_credits.c.creator_id.in_(ids)))
        await conn.execute(delete(creator_aliases).where(creator_aliases.c.creator_id.in_(ids)))
        await conn.execute(
            delete(creator_external_ids).where(creator_external_ids.c.creator_id.in_(ids))
        )
        await conn.execute(delete(creators).where(creators.c.id.in_(ids)))
        for table, key in (
            (creators, "creators"),
            (creator_aliases, "creator_aliases"),
            (creator_external_ids, "creator_external_ids"),
            (work_credits, "work_credits"),
        ):
            await _insert(conn, table, snapshot[key])
    else:
        ids = [log.winner_id, *(uuid.UUID(item) for item in log.loser_ids)]
        await conn.execute(delete(entries).where(entries.c.work_id.in_(ids)))
        await conn.execute(delete(opinions).where(opinions.c.work_id.in_(ids)))
        await conn.execute(delete(work_credits).where(work_credits.c.work_id.in_(ids)))
        await conn.execute(delete(provider_items).where(provider_items.c.work_id.in_(ids)))
        await conn.execute(delete(external_ids).where(external_ids.c.work_id.in_(ids)))
        await conn.execute(
            update(works).where(works.c.parent_work_id.in_(ids)).values(parent_work_id=None)
        )
        await conn.execute(delete(works).where(works.c.id.in_(ids)))
        await _insert(conn, works, snapshot["works"])
        # Parent rows exist already; only their parent pointer changed during a merge.
        for parent in snapshot["parent_works"]:
            await conn.execute(
                update(works)
                .where(works.c.id == _uuid(parent["id"]))
                .values(parent_work_id=_uuid(parent["parent_work_id"]))
            )
        for table, key in (
            (external_ids, "external_ids"),
            (provider_items, "provider_items"),
            (entries, "entries"),
            (opinions, "opinions"),
            (work_credits, "work_credits"),
        ):
            await _insert(conn, table, snapshot[key])
    for queue in snapshot.get("resolution_queue", []):
        await conn.execute(
            update(resolution_queue)
            .where(resolution_queue.c.id == queue["id"])
            .values(**_typed(queue))
        )


async def _insert(conn: Any, table: Any, rows: list[dict[str, Any]]) -> None:
    if rows:
        await conn.execute(table.insert(), [_typed(row) for row in rows])


def _uuid(value: Any) -> uuid.UUID | None:
    return None if value is None or isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _typed(row: dict[str, Any]) -> dict[str, Any]:
    """Recover the two Python types SQLite needs after JSON snapshot serialization."""
    result: dict[str, Any] = {}
    for key, value in row.items():
        if value is None:
            result[key] = None
        elif key in {"id", "work_id", "creator_id", "parent_work_id", "winner_id"} and len(
            str(value)
        ) in {32, 36}:
            result[key] = _uuid(value)
        elif key.endswith("_at") and isinstance(value, str):
            result[key] = datetime.fromisoformat(value)
        else:
            result[key] = value
    return result
