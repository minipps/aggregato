"""retain complete child diagnostics and bounded HTTP responses"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_runs") as batch:
        batch.add_column(sa.Column("log", sa.Text(), nullable=True))
        batch.add_column(sa.Column("raw_responses", sa.JSON(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("run diagnostics are forward-only")
