"""index entries by provider item

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_entries_provider_item_id", "entries", ["provider_item_id"])


def downgrade() -> None:
    raise NotImplementedError("provider-item lookup is part of the forward-only archive")
