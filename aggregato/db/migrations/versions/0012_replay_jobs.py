"""add durable worker-owned replay requests

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("import_jobs", sa.Column("lineage_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column("import_jobs", sa.Column("lease_owner", sa.String(length=64), nullable=True))
    op.add_column(
        "import_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("import_jobs", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("import_jobs", sa.Column("error_class", sa.String(length=32), nullable=True))
    op.add_column(
        "import_jobs", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_table(
        "replay_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("failure_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("lineage_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column("lease_owner", sa.String(length=64)),
        sa.ForeignKeyConstraint(["failure_id"], ["ingest_failures.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_replay_jobs_provider_pending",
        "replay_jobs",
        ["provider_id", "finished_at", "failed_at", "id"],
    )


def downgrade() -> None:
    raise NotImplementedError("replay requests are forward-only")
