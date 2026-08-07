"""add a physical uniqueness key for events without provider-native IDs

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-07
"""

from collections.abc import Sequence
import json

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("entries", sa.Column("subject_ref_key", sa.Text(), nullable=True))
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, provider_item_id, kind, logged_at, subject_ref "
            "FROM entries WHERE native_id IS NULL"
        )
    ).fetchall()
    keys: set[tuple[object, ...]] = set()
    updates: list[dict[str, object]] = []
    for row in rows:
        subject = row.subject_ref
        if isinstance(subject, str):
            subject = json.loads(subject)
        key = json.dumps(subject, sort_keys=True, separators=(",", ":")) if subject else "{}"
        identity = (row.provider_item_id, row.kind, row.logged_at, key)
        if identity in keys:
            raise RuntimeError(
                "entries contain duplicate no-native events; resolve them before applying 0013"
            )
        keys.add(identity)
        updates.append({"id": row.id, "key": key})
    for values in updates:
        conn.execute(
            sa.text("UPDATE entries SET subject_ref_key=:key WHERE id=:id"), values
        )
    op.create_index(
        "uq_entries_no_native_event",
        "entries",
        ["provider_item_id", "kind", "logged_at", "subject_ref_key"],
        unique=True,
        sqlite_where=sa.text("native_id IS NULL AND subject_ref_key IS NOT NULL"),
        postgresql_where=sa.text("native_id IS NULL AND subject_ref_key IS NOT NULL"),
    )


def downgrade() -> None:
    raise NotImplementedError("no-native event identity is forward-only")
