"""retain provider-native identity on captured ingest failures

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ingest_failures") as batch:
        batch.add_column(sa.Column("native_id", sa.Text(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError(
        "failure native identities are forward-only; restore the pre-migration backup"
    )
