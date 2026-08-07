"""make one canonical external identifier resolve to one work

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            "SELECT namespace, value, COUNT(*) AS count "
            "FROM external_ids GROUP BY namespace, value HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if duplicates:
        sample = ", ".join(f"{row.namespace}:{row.value}" for row in duplicates[:5])
        raise RuntimeError(
            "external_ids contains identifiers assigned to multiple works; merge or repair "
            f"these conflicts before applying 0014 ({sample})"
        )
    op.create_index(
        "uq_external_ids_namespace_value_global",
        "external_ids",
        ["namespace", "value"],
        unique=True,
    )


def downgrade() -> None:
    raise NotImplementedError("global external identifier identity is forward-only")
