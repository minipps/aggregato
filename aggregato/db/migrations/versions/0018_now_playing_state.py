"""persist transient provider now-playing state

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep this migration's types frozen rather than importing the live schema aliases. ``JSONB`` is
# the Postgres variant of the portable JSON column used by the current schema.
JSON_COL = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)


def upgrade() -> None:
    """Add nullable current-item state without changing existing provider scheduling state."""
    op.add_column("provider_state", sa.Column("now_playing_item", JSON_COL, nullable=True))
    op.add_column(
        "provider_state", sa.Column("now_playing_changed_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "provider_state", sa.Column("now_playing_checked_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "provider_state", sa.Column("now_playing_next_poll_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "provider_state",
        sa.Column(
            "now_playing_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "provider_state", sa.Column("now_playing_config_fingerprint", sa.String(length=64))
    )


def downgrade() -> None:
    raise NotImplementedError("now-playing state is part of the forward-only provider contract")
