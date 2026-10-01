"""Keep manual creator splits authoritative during ingestion resyncs

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-30
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable UUID columns are directly addable on both supported dialects; avoid rebuilding this
    # table so existing credits and its foreign keys stay in place.
    op.add_column(
        "work_credits",
        sa.Column("manual_from_creator_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    _backfill_active_splits()


def _backfill_active_splits() -> None:
    """Recover the original match for splits already present in merge_log."""
    bind = op.get_bind()
    logs = sa.table(
        "merge_log",
        sa.column("id", sa.BigInteger()),
        sa.column("subject", sa.String()),
        sa.column("operation", sa.String()),
        sa.column("performed_at", sa.DateTime(timezone=True)),
        sa.column("winner_id", sa.Uuid(as_uuid=True)),
        sa.column("loser_ids", sa.JSON()),
        sa.column("moved_credit_ids", sa.JSON()),
        sa.column("undone_at", sa.DateTime(timezone=True)),
        sa.column("snapshot", sa.JSON()),
    )
    credits = sa.table(
        "work_credits",
        sa.column("id", sa.BigInteger()),
        sa.column("creator_id", sa.Uuid(as_uuid=True)),
        sa.column("link_confidence", sa.String()),
        sa.column("manual_from_creator_id", sa.Uuid(as_uuid=True)),
    )
    creator_merges = bind.execute(
        sa.select(logs.c.winner_id, logs.c.loser_ids).where(
            logs.c.subject == "creator",
            logs.c.operation == "merge",
            logs.c.undone_at.is_(None),
        )
    )
    successors: dict[uuid.UUID, uuid.UUID] = {}
    for winner, losers in creator_merges:
        winner_uuid = _as_uuid(winner)
        if winner_uuid is None:
            continue
        for loser in losers or []:
            loser_uuid = _as_uuid(loser)
            if loser_uuid is not None:
                successors[loser_uuid] = winner_uuid

    def live_creator_id(creator_id: uuid.UUID) -> uuid.UUID:
        seen: set[uuid.UUID] = set()
        while creator_id in successors and creator_id not in seen:
            seen.add(creator_id)
            creator_id = successors[creator_id]
        return creator_id

    active_splits = bind.execute(
        sa.select(
            logs.c.moved_credit_ids,
            logs.c.snapshot,
            logs.c.winner_id,
        )
        .where(
            logs.c.subject == "creator",
            logs.c.operation == "split",
            logs.c.undone_at.is_(None),
        )
        .order_by(logs.c.performed_at, logs.c.id)
    )
    original_by_credit: dict[int, uuid.UUID] = {}
    destination_by_credit: dict[int, uuid.UUID] = {}
    for moved_credit_ids, snapshot, destination_id in active_splits:
        destination_uuid = _as_uuid(destination_id)
        if not isinstance(snapshot, dict) or destination_uuid is None:
            continue
        original_by_id: dict[int, uuid.UUID] = {}
        for row in snapshot.get("work_credits", []):
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            source_uuid = _as_uuid(row.get("manual_from_creator_id") or row.get("creator_id"))
            if source_uuid is not None:
                original_by_id[int(row["id"])] = source_uuid
        for raw_id in moved_credit_ids or []:
            credit_id = int(raw_id)
            source_id = original_by_id.get(credit_id)
            if source_id is None:
                continue
            original_by_credit.setdefault(credit_id, source_id)
            destination_by_credit[credit_id] = destination_uuid

    for credit_id, source_id in original_by_credit.items():
        live_destination = live_creator_id(destination_by_credit[credit_id])
        current_creator = bind.execute(
            sa.select(credits.c.creator_id).where(credits.c.id == credit_id)
        ).scalar_one_or_none()
        current_creator_uuid = _as_uuid(current_creator)
        if current_creator_uuid is None or current_creator_uuid != live_destination:
            continue
        bind.execute(
            sa.update(credits)
            .where(credits.c.id == credit_id, credits.c.manual_from_creator_id.is_(None))
            .values(
                link_confidence="manual",
                manual_from_creator_id=source_id,
            )
        )


def _as_uuid(value: object) -> uuid.UUID | None:
    """Accept UUIDs in SQLite's hex form and serialized snapshots' hyphenated form."""
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def downgrade() -> None:
    raise NotImplementedError("manual creator corrections are part of the forward-only archive")
