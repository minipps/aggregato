"""rename the tv_series and anime_series media types to tv and anime

The ``_series`` suffix existed to tell those types apart from ``tv_season`` and ``anime_season``.
0005 retired the season types, so since then the suffix has distinguished them from nothing.

Unlike 0005, this revision is a pure rename and therefore **fully reversible**: 0005 could not
un-merge the seasons it folded into their series, so its ``downgrade`` could only widen the CHECK
back; here ``downgrade`` restores the exact prior values.

Two stores hold the vocabulary, and both are rewritten:

* ``works.media_type``, pinned by a CHECK constraint that has to be replaced alongside the values.
* ``merge_log.snapshot``, which keeps a JSON copy of the pre-operation ``works`` rows so an undo can
  restore them . Renaming only the live rows would leave every undo of a TV or anime merge
  to fail against the new CHECK — a rename that quietly destroyed the undo history it never touched.

Re-runnable, which matters because the SQLite path commits mid-revision (see ``autocommit_block``
below): both rewrites match on the old value, so a second pass over already-renamed data is a no-op.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-30
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from aggregato.db.types, following 0001: a later change to a type
# alias must not retroactively change what this revision reads and writes.
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_MERGE_LOG = sa.table("merge_log", sa.column("id", sa.Integer()), sa.column("snapshot", _JSON))

_BEFORE = (
    "media_type IN ('film', 'tv_series', 'book', 'comic', 'manga', 'anime_series', 'album', "
    "'track', 'game', 'podcast', 'podcast_episode', 'other')"
)
_AFTER = (
    "media_type IN ('film', 'tv', 'book', 'comic', 'manga', 'anime', 'album', 'track', 'game', "
    "'podcast', 'podcast_episode', 'other')"
)
_FORWARD: Mapping[str, str] = {"tv_series": "tv", "anime_series": "anime"}
_BACKWARD: Mapping[str, str] = {new: old for old, new in _FORWARD.items()}


def upgrade() -> None:
    _rename(_FORWARD, check=_AFTER)


def downgrade() -> None:
    _rename(_BACKWARD, check=_BEFORE)


def _rename(renames: Mapping[str, str], *, check: str) -> None:
    """Rewrite every stored media type, leaving ``check`` as the enforced vocabulary."""
    _rewrite_snapshots(renames)
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_rewrite_works(renames, check)
    else:
        _rewrite_works(renames, check)


def _rewrite_works(renames: Mapping[str, str], check: str) -> None:
    """Drop the CHECK, rewrite the values, put the new CHECK back. No table rebuild involved."""
    # The check naming convention includes the existing constraint name; ``op.f`` preserves the
    # frozen name instead of producing ``ck_works_ck_works_media_type`` on PostgreSQL.
    op.drop_constraint(op.f("ck_works_media_type"), "works", type_="check")
    _update_media_types(renames)
    op.create_check_constraint(op.f("ck_works_media_type"), "works", check)


def _sqlite_rewrite_works(renames: Mapping[str, str], check: str) -> None:
    """The same three steps, around SQLite's inability to alter either constraint in place."""
    # SQLite enforces the outgoing CHECK on UPDATE and cannot drop it without rebuilding the table.
    # The alternative to suspending it is widening the CHECK to a union of both vocabularies first,
    # which costs a second rebuild — and every rebuild is another chance to fire the cascade guarded
    # against below. Unlike `foreign_keys`, this pragma is honoured inside a transaction, so the
    # UPDATEs stay in the revision's own transaction.
    op.execute("PRAGMA ignore_check_constraints=ON")
    try:
        _update_media_types(renames)
    finally:
        op.execute("PRAGMA ignore_check_constraints=OFF")

    # The rebuild DROPs the original `works`, and engine.py runs with foreign_keys=ON: DROP TABLE
    # performs an implicit DELETE FROM, which fires every ON DELETE CASCADE pointing at works —
    # entries, opinions, work_credits, provider_items. Without this the revision would delete the
    # log it is only renaming, which is the failure 0005 had to be built around.
    # PRAGMA foreign_keys is a no-op inside a transaction, hence autocommit_block;
    # defer_foreign_keys does not help, because the implicit delete still runs the cascade actions.
    context = op.get_context()
    with context.autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
    try:
        # Reflection does not carry the old CHECK over, so creating the new one is also what drops
        # the old. Checks are armed again by now on purpose: the rebuild's copy re-validates every
        # row, so a media type outside the new vocabulary fails here rather than surviving unseen.
        with op.batch_alter_table("works") as batch:
            batch.create_check_constraint("media_type", check)
    finally:
        with context.autocommit_block():
            op.execute("PRAGMA foreign_keys=ON")


def _update_media_types(renames: Mapping[str, str]) -> None:
    statement = sa.text("UPDATE works SET media_type = :new WHERE media_type = :old")
    for old, new in renames.items():
        op.execute(statement.bindparams(old=old, new=new))


def _rewrite_snapshots(renames: Mapping[str, str]) -> None:
    """Rename the media types inside merge_log's pre-operation row snapshots ."""
    bind = op.get_bind()
    rows = bind.execute(sa.select(_MERGE_LOG.c.id, _MERGE_LOG.c.snapshot)).fetchall()
    for row_id, snapshot in rows:
        # SQLite hands back what the JSON type deserialized; a Postgres driver may have done it
        # already. Both shapes reach here, so neither is assumed.
        document = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        if not _retype(document, renames):
            continue
        bind.execute(_MERGE_LOG.update().where(_MERGE_LOG.c.id == row_id).values(snapshot=document))


def _retype(node: Any, renames: Mapping[str, str]) -> bool:
    """Rename every ``media_type`` value anywhere in a snapshot, reporting whether anything changed.

    A walk rather than a query against the snapshot's known keys: it holds one list of rows per
    table the operation touched (``works``, ``parent_works``, …), and a walk cannot be wrong about
    that shape or go stale when a later merge records another table.
    """
    changed = False
    if isinstance(node, dict):
        current = node.get("media_type")
        if isinstance(current, str) and current in renames:
            node["media_type"] = renames[current]
            changed = True
        for value in node.values():
            changed = _retype(value, renames) or changed
    elif isinstance(node, list):
        for item in node:
            changed = _retype(item, renames) or changed
    return changed
