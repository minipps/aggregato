"""record live sync-run progress for API and websocket consumers

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PHASE_CHECK = (
    "phase IN ('starting', 'checking', 'replaying', 'fetching', 'ingesting', 'finalizing', "
    "'finished', 'failed')"
)


def upgrade() -> None:
    """Add progress fields without changing any existing run outcome or payload."""
    context = op.get_context()
    sqlite = context.bind.dialect.name == "sqlite"
    if sqlite:
        # Batch mode drops and recreates the source table. With foreign keys enabled, SQLite's
        # implicit delete cascades into ingest_failures, which would erase the history this
        # additive migration is meant to preserve.
        with context.autocommit_block():
            op.execute("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("sync_runs") as batch:
            batch.add_column(
                sa.Column(
                    "phase",
                    sa.String(length=16),
                    nullable=False,
                    server_default=sa.text("'starting'"),
                )
            )
            batch.add_column(sa.Column("progress_total", sa.Integer(), nullable=True))
            batch.add_column(
                sa.Column(
                    "checkpoint_count", sa.Integer(), nullable=False, server_default=sa.text("0")
                )
            )
            batch.add_column(
                sa.Column("last_checkpoint_at", sa.DateTime(timezone=True), nullable=True)
            )
            batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
            batch.add_column(
                sa.Column(
                    "progress_revision", sa.Integer(), nullable=False, server_default=sa.text("0")
                )
            )
            batch.create_check_constraint("phase", _PHASE_CHECK)
    finally:
        if sqlite:
            with context.autocommit_block():
                op.execute("PRAGMA foreign_keys=ON")
    # Existing terminal rows have no live phase to observe. Backfill a truthful terminal phase and
    # timestamp so history does not render every old successful run as perpetually "starting".
    op.execute(
        "UPDATE sync_runs SET phase = 'finished' WHERE status IN ('success', 'partial')"
    )
    op.execute("UPDATE sync_runs SET phase = 'failed' WHERE status = 'failed'")
    op.execute("UPDATE sync_runs SET updated_at = COALESCE(finished_at, started_at)")


def downgrade() -> None:
    raise NotImplementedError("live sync progress is part of the forward-only run contract")
