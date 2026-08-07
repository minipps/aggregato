"""retain rejected full-run observations without changing the sanity baseline

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_state",
        sa.Column("last_failed_window_item_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    raise NotImplementedError("sanity observations are retained by a forward-only migration")
