"""phase 4 read-path indexes

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add indexes for the live log keyset paths and score-fact grouping."""
    op.create_index(
        "ix_entries_active_logged_at_id",
        "entries",
        ["deleted_at", "subject_ref", sa.text("logged_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_entries_active_ingested_at_id",
        "entries",
        ["deleted_at", "subject_ref", sa.text("ingested_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_opinions_active_provider_item_rating",
        "opinions",
        ["deleted_at", "provider_item_id", sa.text("rating_normalized DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Remove the Phase 4 read-path indexes."""
    op.drop_index("ix_opinions_active_provider_item_rating", table_name="opinions")
    op.drop_index("ix_entries_active_ingested_at_id", table_name="entries")
    op.drop_index("ix_entries_active_logged_at_id", table_name="entries")
