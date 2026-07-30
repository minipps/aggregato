"""carry an operator's requested sync mode through to dispatch

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("provider_state", sa.Column("requested_mode", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("provider_state", "requested_mode")
