"""retire unsupported podcast media types

Legacy rows are retyped as ``other`` so removing an unavailable integration never destroys an
operator's existing log. Merge snapshots are rewritten too: undo restores the stored rows verbatim.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-07
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the live enum: migrations describe the vocabulary at the
# point they ran and must not change when the current domain vocabulary changes.
_CHECK = (
    "media_type IN ('film', 'tv', 'book', 'comic', 'manga', 'anime', 'album', 'track', 'game', "
    "'other')"
)
_RETIRED: Mapping[str, str] = {
    "podcast": "other",
    "podcast_episode": "other",
}
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_MERGE_LOG = sa.table("merge_log", sa.column("id", sa.Integer()), sa.column("snapshot", _JSON))


def upgrade() -> None:
    _rewrite_snapshots(_RETIRED)
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_rewrite_works(_RETIRED)
    else:
        _rewrite_works(_RETIRED)


def downgrade() -> None:
    raise NotImplementedError("retired media types are forward-only")


def _rewrite_works(retypes: Mapping[str, str]) -> None:
    """Drop the old CHECK, retype legacy rows, and install the current vocabulary."""
    op.drop_constraint(op.f("ck_works_media_type"), "works", type_="check")
    _update_media_types(retypes)
    op.create_check_constraint(op.f("ck_works_media_type"), "works", _CHECK)


def _sqlite_rewrite_works(retypes: Mapping[str, str]) -> None:
    """Rebuild SQLite's CHECK without cascading away rows owned by ``works``."""
    # The outgoing CHECK rejects the retype, so suspend only CHECK evaluation for the update.
    op.execute("PRAGMA ignore_check_constraints=ON")
    try:
        _update_media_types(retypes)
    finally:
        op.execute("PRAGMA ignore_check_constraints=OFF")

    # Dropping the source table during a batch rebuild would otherwise fire its ON DELETE CASCADE
    # relationships. This is the same guard used by the earlier vocabulary migrations.
    context = op.get_context()
    with context.autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("works") as batch:
            batch.create_check_constraint("media_type", _CHECK)
    finally:
        with context.autocommit_block():
            op.execute("PRAGMA foreign_keys=ON")


def _update_media_types(retypes: Mapping[str, str]) -> None:
    statement = sa.text("UPDATE works SET media_type = :new WHERE media_type = :old")
    for old, new in retypes.items():
        op.execute(statement.bindparams(old=old, new=new))


def _rewrite_snapshots(retypes: Mapping[str, str]) -> None:
    """Retype legacy values inside merge-log snapshots used by undo."""
    bind = op.get_bind()
    rows = bind.execute(sa.select(_MERGE_LOG.c.id, _MERGE_LOG.c.snapshot)).fetchall()
    for row_id, snapshot in rows:
        document = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        if _retype(document, retypes):
            bind.execute(
                _MERGE_LOG.update().where(_MERGE_LOG.c.id == row_id).values(snapshot=document)
            )


def _retype(node: Any, retypes: Mapping[str, str]) -> bool:
    changed = False
    if isinstance(node, dict):
        current = node.get("media_type")
        if isinstance(current, str) and current in retypes:
            node["media_type"] = retypes[current]
            changed = True
        for value in node.values():
            changed = _retype(value, retypes) or changed
    elif isinstance(node, list):
        for item in node:
            changed = _retype(item, retypes) or changed
    return changed
