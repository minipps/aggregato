"""persist the lineage of an operator-requested sync

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_state",
        sa.Column("requested_lineage_id", sa.Uuid(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    raise NotImplementedError("requested lineage is part of the forward-only state machine")
