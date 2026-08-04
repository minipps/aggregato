"""repair duplicate asserted creator identifiers and enforce their global uniqueness

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = sa.Uuid(as_uuid=True)
_CREATORS = sa.table(
    "creators", sa.column("id", _UUID), sa.column("created_at", sa.DateTime(timezone=True))
)
_ALIASES = sa.table(
    "creator_aliases",
    sa.column("id", sa.BigInteger()),
    sa.column("creator_id", _UUID),
    sa.column("normalized", sa.Text()),
    sa.column("media_family", sa.String()),
)
_EXTERNAL_IDS = sa.table(
    "creator_external_ids",
    sa.column("id", sa.BigInteger()),
    sa.column("creator_id", _UUID),
    sa.column("namespace", sa.String()),
    sa.column("value", sa.Text()),
)
_CREDITS = sa.table(
    "work_credits",
    sa.column("id", sa.BigInteger()),
    sa.column("work_id", _UUID),
    sa.column("creator_id", _UUID),
    sa.column("role", sa.String()),
    sa.column("source", sa.String()),
)


def upgrade() -> None:
    """Collapse connected duplicate-ID clusters before making the invariant physical."""
    bind = op.get_bind()
    duplicate_groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in bind.execute(
        sa.select(_EXTERNAL_IDS.c.namespace, _EXTERNAL_IDS.c.value, _EXTERNAL_IDS.c.creator_id)
    ):
        duplicate_groups[(row.namespace, row.value)].append(row.creator_id)

    parent: dict[Any, Any] = {}

    def find(value: Any) -> Any:
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    def union(left: Any, right: Any) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for ids in duplicate_groups.values():
        unique_ids = list(dict.fromkeys(ids))
        for creator_id in unique_ids[1:]:
            union(unique_ids[0], creator_id)

    if parent:
        created_at = {
            row.id: row.created_at
            for row in bind.execute(sa.select(_CREATORS.c.id, _CREATORS.c.created_at))
            if row.id in parent
        }
        components: dict[Any, list[Any]] = defaultdict(list)
        for creator_id in parent:
            components[find(creator_id)].append(creator_id)
        for ids in components.values():
            winner = min(ids, key=lambda creator_id: (created_at[creator_id], str(creator_id)))
            for loser in ids:
                if loser != winner:
                    _merge_creator(bind, winner=winner, loser=loser)

    with op.batch_alter_table("creator_external_ids") as batch:
        batch.drop_constraint("uq_creator_external_ids_namespace_value_creator", type_="unique")
        batch.create_unique_constraint(
            "uq_creator_external_ids_namespace_value", ["namespace", "value"]
        )


def _merge_creator(bind: Any, *, winner: Any, loser: Any) -> None:
    """Move a duplicate identity without violating any child-table uniqueness constraint."""
    winner_credits = {
        (row.work_id, row.role, row.source)
        for row in bind.execute(
            sa.select(_CREDITS.c.work_id, _CREDITS.c.role, _CREDITS.c.source).where(
                _CREDITS.c.creator_id == winner
            )
        )
    }
    for row in bind.execute(
        sa.select(_CREDITS.c.id, _CREDITS.c.work_id, _CREDITS.c.role, _CREDITS.c.source).where(
            _CREDITS.c.creator_id == loser
        )
    ):
        if (row.work_id, row.role, row.source) in winner_credits:
            bind.execute(sa.delete(_CREDITS).where(_CREDITS.c.id == row.id))
        else:
            bind.execute(
                sa.update(_CREDITS).where(_CREDITS.c.id == row.id).values(creator_id=winner)
            )

    winner_aliases = {
        (row.normalized, row.media_family)
        for row in bind.execute(
            sa.select(_ALIASES.c.normalized, _ALIASES.c.media_family).where(
                _ALIASES.c.creator_id == winner
            )
        )
    }
    for row in bind.execute(
        sa.select(_ALIASES.c.id, _ALIASES.c.normalized, _ALIASES.c.media_family).where(
            _ALIASES.c.creator_id == loser
        )
    ):
        if (row.normalized, row.media_family) in winner_aliases:
            bind.execute(sa.delete(_ALIASES).where(_ALIASES.c.id == row.id))
        else:
            bind.execute(
                sa.update(_ALIASES).where(_ALIASES.c.id == row.id).values(creator_id=winner)
            )

    winner_ids = {
        (row.namespace, row.value)
        for row in bind.execute(
            sa.select(_EXTERNAL_IDS.c.namespace, _EXTERNAL_IDS.c.value).where(
                _EXTERNAL_IDS.c.creator_id == winner
            )
        )
    }
    for row in bind.execute(
        sa.select(_EXTERNAL_IDS.c.id, _EXTERNAL_IDS.c.namespace, _EXTERNAL_IDS.c.value).where(
            _EXTERNAL_IDS.c.creator_id == loser
        )
    ):
        if (row.namespace, row.value) in winner_ids:
            bind.execute(sa.delete(_EXTERNAL_IDS).where(_EXTERNAL_IDS.c.id == row.id))
        else:
            bind.execute(
                sa.update(_EXTERNAL_IDS)
                .where(_EXTERNAL_IDS.c.id == row.id)
                .values(creator_id=winner)
            )
            winner_ids.add((row.namespace, row.value))
    bind.execute(sa.delete(_CREATORS).where(_CREATORS.c.id == loser))


def downgrade() -> None:
    raise NotImplementedError(
        "creator identity repair is forward-only; restore the pre-migration backup"
    )
