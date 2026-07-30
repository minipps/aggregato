"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    # Altering a column or a constraint on a table other tables reference? SQLite rebuilds it by
    # DROPping the original, which cascades child rows away. Read data-model.md §6 "Rebuilding a
    # table on SQLite deletes its children" first, and land a survival test with this revision.
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    # Migrations are forward-only (data-model.md §6). See the initial revision for why.
    raise NotImplementedError("migrations are forward-only")
