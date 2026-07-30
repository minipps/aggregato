"""fold tv_season and anime_season into their series types

Seasons are no longer a media type of their own: almost no platform logs against a season, so the
type only ever carried provider-specific noise. Existing season rows keep their `parent_work_id` and
`sequence_number`, so a season under a series is still identifiable — it is just typed as a series.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WITH_SEASONS = (
    "media_type IN ('film', 'tv_series', 'tv_season', 'book', 'comic', 'manga', 'anime_series', "
    "'anime_season', 'album', 'track', 'game', 'podcast', 'podcast_episode', 'other')"
)
_WITHOUT_SEASONS = (
    "media_type IN ('film', 'tv_series', 'book', 'comic', 'manga', 'anime_series', 'album', "
    "'track', 'game', 'podcast', 'podcast_episode', 'other')"
)


def _replace_check(expression: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        # Batch mode rebuilds the table; SQLite reflection does not carry the old CHECK over, so
        # creating the new one is also what drops the old.
        #
        # The rebuild DROPs the original `works`, and engine.py runs with foreign_keys=ON: DROP
        # TABLE performs an implicit DELETE FROM, which fires every ON DELETE CASCADE pointing at
        # works — entries, opinions, work_credits, provider_items. Rebuilding the table would
        # otherwise empty the log. PRAGMA foreign_keys is a no-op inside a transaction, hence
        # autocommit_block; defer_foreign_keys does not help, because the implicit delete still
        # runs the cascade actions.
        context = op.get_context()
        with context.autocommit_block():
            op.execute("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("works") as batch:
                batch.create_check_constraint("media_type", expression)
        finally:
            with context.autocommit_block():
                op.execute("PRAGMA foreign_keys=ON")
        return
    op.drop_constraint("ck_works_media_type", "works", type_="check")
    op.create_check_constraint("media_type", "works", expression)


def upgrade() -> None:
    op.execute("UPDATE works SET media_type = 'tv_series' WHERE media_type = 'tv_season'")
    op.execute("UPDATE works SET media_type = 'anime_series' WHERE media_type = 'anime_season'")
    _replace_check(_WITHOUT_SEASONS)


def downgrade() -> None:
    # The rows cannot be un-merged, but widening the CHECK back is what lets 0005 be re-run.
    _replace_check(_WITH_SEASONS)
