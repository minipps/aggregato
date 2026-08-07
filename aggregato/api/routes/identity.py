"""Manual identity correction and reversible merge-log operations ."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from starlette.requests import Request

from aggregato.api.clock import now as request_now
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
from aggregato.db.search import SearchKind, rebuild_work_document, unindex_document
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
                conn, winner_id=id, loser_ids=body.loser_ids, now=request_now(request)
            )
            return _entry(row)
    except (ValueError, LookupError) as exc:
        raise ProblemError(409, "Conflict", str(exc)) from exc


@router.post("/creators/{id}/merge", response_model=MergeLogEntry)
async def merge_creator(request: Request, id: uuid.UUID, body: MergeRequest) -> MergeLogEntry:
    try:
        async with transaction(request.app.state.engine) as conn:
            row = await merge_creators(
                conn, winner_id=id, loser_ids=body.loser_ids, now=request_now(request)
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
                now=request_now(request),
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
            await _assert_undo_safe(conn, row)
            await _restore(conn, row)
        except Exception as exc:
            raise ProblemError(
                409, "Conflict", "This operation can no longer be safely undone."
            ) from exc
        result = await conn.execute(
            update(merge_log)
            .where(merge_log.c.id == id, merge_log.c.undone_at.is_(None))
            .values(undone_at=request_now(request))
        )
        if result.rowcount != 1:
            raise ProblemError(409, "Conflict", "This operation has already been undone.")
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
        for work_id in ids:
            await unindex_document(conn, SearchKind.WORK_TITLE, str(work_id))
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
        for work in snapshot["works"]:
            work_id = _uuid(work["id"])
            assert work_id is not None
            await rebuild_work_document(conn, work_id)
    for queue in snapshot.get("resolution_queue", []):
        await conn.execute(
            update(resolution_queue)
            .where(resolution_queue.c.id == queue["id"])
            .values(**_typed(queue))
        )


async def _insert(conn: Any, table: Any, rows: list[dict[str, Any]]) -> None:
    if rows:
        await conn.execute(table.insert(), [_typed(row) for row in rows])


async def _assert_undo_safe(conn: Any, log: Any) -> None:
    """Reject undo when a later write would be erased by restoring the snapshot.

    Merge rewrites foreign keys and removes duplicate rows as part of its own operation. The check
    therefore allows those merge-owned columns, but rejects new rows, missing rows, or a newer
    timestamp in every affected table. It is optimistic concurrency for an operation whose inverse
    is otherwise necessarily destructive.
    """
    snapshot = log.snapshot
    performed_at = _timestamp(log.performed_at)
    if set(snapshot) == {"resolution_queue"}:
        await _assert_snapshot_rows(
            conn, resolution_queue, snapshot["resolution_queue"], performed_at
        )
        return
    if log.operation == "split":
        await _assert_snapshot_rows(conn, creators, snapshot["creators"], performed_at)
        moved = [int(item) for item in log.moved_credit_ids or []]
        rows = list(
            await conn.execute(
                select(work_credits).where(work_credits.c.creator_id == log.winner_id)
            )
        )
        if {int(row.id) for row in rows} != set(moved):
            raise ValueError("credits changed after the split")
        await _assert_snapshot_rows(
            conn,
            work_credits,
            snapshot["work_credits"],
            performed_at,
            current_rows=rows,
            ignored_columns={"creator_id"},
        )
        return

    subject_ids = [log.winner_id, *(uuid.UUID(item) for item in log.loser_ids)]
    specs: tuple[tuple[Any, str, list[uuid.UUID], str, set[Any], set[str]], ...]
    if log.subject == "creator":
        specs = (
            (creators, "id", subject_ids, "creators", set(), set()),
            (
                creator_aliases,
                "creator_id",
                subject_ids,
                "creator_aliases",
                _duplicate_ids(
                    snapshot["creator_aliases"],
                    "creator_id",
                    ("normalized", "media_family"),
                    subject_ids[0],
                ),
                {"creator_id"},
            ),
            (
                creator_external_ids,
                "creator_id",
                subject_ids,
                "creator_external_ids",
                _duplicate_ids(
                    snapshot["creator_external_ids"],
                    "creator_id",
                    ("namespace", "value"),
                    subject_ids[0],
                ),
                {"creator_id"},
            ),
            (
                work_credits,
                "creator_id",
                subject_ids,
                "work_credits",
                _duplicate_ids(
                    snapshot["work_credits"],
                    "creator_id",
                    ("work_id", "role", "source"),
                    subject_ids[0],
                ),
                {"creator_id"},
            ),
        )
    else:
        specs = (
            (works, "id", subject_ids, "works", set(subject_ids[1:]), set()),
            (works, "parent_work_id", subject_ids, "parent_works", set(), {"parent_work_id"}),
            (
                external_ids,
                "work_id",
                subject_ids,
                "external_ids",
                _duplicate_ids(
                    snapshot["external_ids"], "work_id", ("namespace", "value"), subject_ids[0]
                ),
                {"work_id"},
            ),
            (provider_items, "work_id", subject_ids, "provider_items", set(), {"work_id"}),
            (entries, "work_id", subject_ids, "entries", set(), {"work_id"}),
            (opinions, "work_id", subject_ids, "opinions", set(), {"work_id"}),
            (
                work_credits,
                "work_id",
                subject_ids,
                "work_credits",
                _duplicate_ids(
                    snapshot["work_credits"],
                    "work_id",
                    ("creator_id", "role", "source"),
                    subject_ids[0],
                ),
                {"work_id"},
            ),
        )
    for table, column_name, ids, key, allow_missing, ignored_columns in specs:
        snapshot_rows = snapshot.get(key, [])
        column = getattr(table.c, column_name)
        current = list(await conn.execute(select(table).where(column.in_(ids))))
        await _assert_snapshot_rows(
            conn,
            table,
            snapshot_rows,
            performed_at,
            current_rows=current,
            allow_missing=allow_missing,
            ignored_columns=ignored_columns,
        )


async def _assert_snapshot_rows(
    conn: Any,
    table: Any,
    snapshot_rows: list[dict[str, Any]],
    performed_at: datetime,
    *,
    current_rows: list[Any] | None = None,
    allow_missing: set[Any] | None = None,
    ignored_columns: set[str] | None = None,
) -> None:
    """Check rows, allowing only the foreign-key rewrites performed by the merge itself."""
    allow_missing_ids = {_identity(value) for value in (allow_missing or set())}
    ignored = ignored_columns or set()
    if current_rows is None:
        ids = [_typed(row)["id"] for row in snapshot_rows]
        current_rows = list(await conn.execute(select(table).where(table.c.id.in_(ids))))
    current_by_id = {_identity(row.id): row for row in current_rows}
    for expected in snapshot_rows:
        key = _identity(expected["id"])
        row = current_by_id.get(key)
        if row is None:
            if key not in allow_missing_ids:
                raise ValueError(f"{table.name} changed after the operation")
            continue
        for name in expected:
            if name == "id" or name in ignored:
                continue
            current_value = getattr(row, name)
            if _normalized(current_value) != _normalized(expected[name]):
                raise ValueError(f"{table.name} changed after the operation")
        for name in ("created_at", "updated_at", "ingested_at", "first_seen_at", "last_seen_at"):
            value = getattr(row, name, None)
            if isinstance(value, datetime) and _timestamp(value) > performed_at:
                raise ValueError(f"{table.name} changed after the operation")
    expected_ids = {_identity(row["id"]) for row in snapshot_rows}
    current_ids = {_identity(row.id) for row in current_rows}
    if not current_ids.issubset(expected_ids):
        raise ValueError(f"{table.name} changed after the operation")


def _duplicate_ids(
    rows: list[dict[str, Any]], owner_column: str, key_columns: tuple[str, ...], winner: Any
) -> set[str]:
    """IDs the merge intentionally removes because the winner already has the same key."""
    winner_keys = {
        tuple(_normalized(row[column]) for column in key_columns)
        for row in rows
        if _identity(row[owner_column]) == _identity(winner)
    }
    return {
        _identity(row["id"])
        for row in rows
        if _identity(row[owner_column]) != _identity(winner)
        and tuple(_normalized(row[column]) for column in key_columns) in winner_keys
    }


def _normalized(value: Any) -> str:
    return json.dumps(jsonable_encoder(value), sort_keys=True, default=str)


def _timestamp(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


def _identity(value: Any) -> str:
    return str(value)


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
