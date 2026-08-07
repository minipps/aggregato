"""initial schema

The 18 tables of data-model.md, plus the dialect-specific search index , which cannot live
in ``schema.py``'s ``MetaData`` because its DDL differs per dialect — so this revision calls
``sync_create_search_index`` instead.

Written out explicitly rather than as ``metadata.create_all()``. A revision must be a *frozen
snapshot*: ``create_all`` would build whatever ``schema.py`` says today, so the first later revision
that adds a column would then try to add it to a table that already has it, and every fresh install
would fail at revision 2. The cost is that this file and ``schema.py`` must be kept in step, which
``alembic revision --autogenerate`` reports (and which the migration test asserts against
``metadata`` directly).

Note that autogenerate silently drops the five expression-based indexes (``logged_at DESC`` and
friends) because SQLite cannot reflect them — they are written by hand below.

Revision ID: 0001
Revises:
Create Date: 2026-07-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from aggregato.db.search import sync_create_search_index

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out here rather than imported from aggregato.db.types, so a later change to a type alias
# cannot retroactively change what this revision built.
UUID = sa.Uuid()
AUTO = sa.BigInteger().with_variant(sa.INTEGER(), "sqlite")
TS = sa.DateTime(timezone=True)
DECIMAL = sa.Numeric(precision=10, scale=4)
JSON_COL = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "creators",
        sa.Column("id", UUID, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sort_name", sa.Text(), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("metadata", JSON_COL, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(
            "kind IN ('person', 'group', 'studio', 'imprint', 'unknown')",
            name=op.f("ck_creators_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_creators")),
    )
    op.create_index("ix_creators_sort_name", "creators", ["sort_name"], unique=False)

    op.create_table(
        "image_cache",
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("bytes_sha256", sa.String(length=64), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("fetched_at", TS, nullable=True),
        sa.Column("failed_at", TS, nullable=True),
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("url_hash", name=op.f("pk_image_cache")),
    )
    op.create_index("ix_image_cache_bytes_sha256", "image_cache", ["bytes_sha256"], unique=False)

    op.create_table(
        "merge_log",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("subject", sa.String(length=16), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("winner_id", UUID, nullable=False),
        sa.Column("loser_ids", JSON_COL, nullable=False),
        sa.Column("moved_credit_ids", JSON_COL, nullable=True),
        sa.Column("performed_at", TS, nullable=False),
        sa.Column("undone_at", TS, nullable=True),
        sa.Column("snapshot", JSON_COL, nullable=False),
        sa.CheckConstraint(
            "subject IN ('work', 'creator')", name=op.f("ck_merge_log_subject")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merge_log")),
    )
    op.create_index(
        "ix_merge_log_performed_at", "merge_log", [sa.text("performed_at DESC")], unique=False
    )

    op.create_table(
        "providers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("acquisition", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("reviewed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("config", JSON_COL, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("last_error", JSON_COL, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(
            "acquisition IN ('api', 'feed', 'export', 'scrape')",
            name=op.f("ck_providers_acquisition"),
        ),
        sa.CheckConstraint(
            "status IN ('disabled', 'idle', 'syncing', 'degraded', 'misconfigured')",
            name=op.f("ck_providers_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
    )

    op.create_table(
        "rating_scales",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("min_value", DECIMAL, nullable=False),
        sa.Column("max_value", DECIMAL, nullable=False),
        sa.Column("step", DECIMAL, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("labels", JSON_COL, nullable=True),
        sa.CheckConstraint(
            "kind IN ('linear', 'ordinal')", name=op.f("ck_rating_scales_kind")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rating_scales")),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("token_fingerprint", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)

    op.create_table(
        "sync_runs",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("lineage_id", UUID, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("items_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_written", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("log_excerpt", sa.Text(), nullable=True),
        sa.Column("cursor_before", JSON_COL, nullable=True),
        sa.Column("cursor_after", JSON_COL, nullable=True),
        sa.CheckConstraint(
            "error_class IN ('auth', 'rate_limit', 'transport', 'parse', 'structure_changed', "
            "'blocked', 'internal')",
            name=op.f("ck_sync_runs_error_class"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failed')",
            name=op.f("ck_sync_runs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index("ix_sync_runs_lineage_id", "sync_runs", ["lineage_id"], unique=False)
    op.create_index(
        "ix_sync_runs_provider_id_started_at",
        "sync_runs",
        ["provider_id", sa.text("started_at DESC")],
        unique=False,
    )

    op.create_table(
        "works",
        sa.Column("id", UUID, nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("sort_title", sa.Text(), nullable=False),
        sa.Column("original_title", sa.Text(), nullable=True),
        sa.Column("release_year", sa.Integer(), nullable=True),
        sa.Column("parent_work_id", UUID, nullable=True),
        sa.Column("sequence_number", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("metadata", JSON_COL, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(
            "media_type IN ('film', 'tv_series', 'tv_season', 'book', 'comic', 'manga', "
            "'anime_series', 'anime_season', 'album', 'track', 'game', 'podcast', "
            "'podcast_episode', 'other')",
            name=op.f("ck_works_media_type"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_work_id"],
            ["works.id"],
            name=op.f("fk_works_parent_work_id_works"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_works")),
    )
    op.create_index(
        "ix_works_media_type_sort_title_release_year",
        "works",
        ["media_type", "sort_title", "release_year"],
        unique=False,
    )
    op.create_index("ix_works_parent_work_id", "works", ["parent_work_id"], unique=False)

    op.create_table(
        "creator_aliases",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("creator_id", UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized", sa.Text(), nullable=False),
        sa.Column("media_family", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "kind IN ('primary', 'alias', 'romanization', 'native', 'credited_as')",
            name=op.f("ck_creator_aliases_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_id"],
            ["creators.id"],
            name=op.f("fk_creator_aliases_creator_id_creators"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_creator_aliases")),
        sa.UniqueConstraint(
            "creator_id", "normalized", "media_family", name="uq_creator_aliases_creator_normalized"
        ),
    )
    op.create_index(
        "ix_creator_aliases_normalized_media_family",
        "creator_aliases",
        ["normalized", "media_family"],
        unique=False,
    )

    op.create_table(
        "creator_external_ids",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("creator_id", UUID, nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "confidence IN ('asserted', 'matched', 'manual')",
            name=op.f("ck_creator_external_ids_confidence"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_id"],
            ["creators.id"],
            name=op.f("fk_creator_external_ids_creator_id_creators"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_creator_external_ids")),
        sa.UniqueConstraint(
            "namespace",
            "value",
            "creator_id",
            name="uq_creator_external_ids_namespace_value_creator",
        ),
    )
    op.create_index(
        "ix_creator_external_ids_namespace_value",
        "creator_external_ids",
        ["namespace", "value"],
        unique=False,
    )

    op.create_table(
        "external_ids",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("work_id", UUID, nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint(
            "confidence IN ('asserted', 'matched', 'manual')",
            name=op.f("ck_external_ids_confidence"),
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["works.id"],
            name=op.f("fk_external_ids_work_id_works"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_ids")),
        sa.UniqueConstraint(
            "namespace", "value", "work_id", name="uq_external_ids_namespace_value_work"
        ),
    )
    op.create_index(
        "ix_external_ids_namespace_value", "external_ids", ["namespace", "value"], unique=False
    )
    op.create_index("ix_external_ids_work_id", "external_ids", ["work_id"], unique=False)

    op.create_table(
        "provider_items",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("native_id", sa.Text(), nullable=False),
        sa.Column("work_id", UUID, nullable=True),
        sa.Column("title_as_given", sa.Text(), nullable=False),
        sa.Column("raw_payload", JSON_COL, nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["works.id"],
            name=op.f("fk_provider_items_work_id_works"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_items")),
        sa.UniqueConstraint(
            "provider_id", "native_id", name="uq_provider_items_provider_id_native_id"
        ),
    )
    op.create_index(
        "ix_provider_items_provider_id_schema_version",
        "provider_items",
        ["provider_id", "schema_version"],
        unique=False,
    )
    op.create_index("ix_provider_items_work_id", "provider_items", ["work_id"], unique=False)

    op.create_table(
        "provider_state",
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("cursor", JSON_COL, nullable=True),
        sa.Column("next_run_at", TS, nullable=True),
        sa.Column("effective_interval_seconds", sa.Integer(), nullable=False),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("retry_step", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_success_at", TS, nullable=True),
        sa.Column("last_window_item_count", sa.Integer(), nullable=True),
        sa.Column("kv", JSON_COL, server_default=sa.text("'{}'"), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_provider_state_provider_id_providers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("provider_id", name=op.f("pk_provider_state")),
    )
    op.create_index(
        "ix_provider_state_next_run_at", "provider_state", ["next_run_at"], unique=False
    )

    op.create_table(
        "ingest_failures",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("sync_run_id", AUTO, nullable=False),
        sa.Column("raw_payload", JSON_COL, nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("resolved_at", TS, nullable=True),
        sa.CheckConstraint(
            "stage IN ('fetch', 'validate', 'normalize', 'write')",
            name=op.f("ck_ingest_failures_stage"),
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            ["sync_runs.id"],
            name=op.f("fk_ingest_failures_sync_run_id_sync_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_failures")),
    )
    op.create_index(
        "ix_ingest_failures_resolved_at", "ingest_failures", ["resolved_at"], unique=False
    )
    op.create_index(
        "ix_ingest_failures_provider_id_created_at",
        "ingest_failures",
        ["provider_id", sa.text("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "work_credits",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("work_id", UUID, nullable=False),
        sa.Column("creator_id", UUID, nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("role_raw", sa.Text(), nullable=True),
        sa.Column("credited_as", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("link_confidence", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "link_confidence IN ('asserted', 'matched', 'manual')",
            name=op.f("ck_work_credits_link_confidence"),
        ),
        sa.CheckConstraint(
            "role IN ('author', 'illustrator', 'translator', 'editor', 'director', 'writer', "
            "'composer', 'performer', 'featured_performer', 'voice', 'narrator', 'studio', "
            "'publisher', 'developer', 'other')",
            name=op.f("ck_work_credits_role"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_id"],
            ["creators.id"],
            name=op.f("fk_work_credits_creator_id_creators"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["works.id"],
            name=op.f("fk_work_credits_work_id_works"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_credits")),
        sa.UniqueConstraint(
            "work_id",
            "creator_id",
            "role",
            "source",
            name="uq_work_credits_work_creator_role_source",
        ),
    )
    op.create_index(
        "ix_work_credits_creator_id_role", "work_credits", ["creator_id", "role"], unique=False
    )
    op.create_index("ix_work_credits_work_id", "work_credits", ["work_id"], unique=False)

    op.create_table(
        "entries",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("work_id", UUID, nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("provider_item_id", AUTO, nullable=False),
        sa.Column("native_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("logged_at", TS, nullable=False),
        sa.Column("logged_precision", sa.String(length=16), nullable=False),
        sa.Column("subject_ref", JSON_COL, nullable=True),
        sa.Column("progress_value", DECIMAL, nullable=True),
        sa.Column("progress_unit", sa.String(length=32), nullable=True),
        sa.Column("metadata", JSON_COL, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("ingested_at", TS, nullable=False),
        sa.Column("deleted_at", TS, nullable=True),
        sa.CheckConstraint(
            "kind IN ('watch', 'rewatch', 'listen', 'read', 'finish', 'progress', 'drop')",
            name=op.f("ck_entries_kind"),
        ),
        sa.CheckConstraint(
            "logged_precision IN ('exact', 'day', 'month', 'year', 'unknown')",
            name=op.f("ck_entries_logged_precision"),
        ),
        sa.CheckConstraint(
            "(progress_value IS NULL) = (progress_unit IS NULL)",
            name=op.f("ck_entries_progress_value_needs_unit"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_item_id"],
            ["provider_items.id"],
            name=op.f("fk_entries_provider_item_id_provider_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"], ["works.id"], name=op.f("fk_entries_work_id_works"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entries")),
    )
    op.create_index("ix_entries_provider_id", "entries", ["provider_id"], unique=False)
    op.create_index("ix_entries_work_id", "entries", ["work_id"], unique=False)
    op.create_index(
        "uq_entries_provider_id_native_id",
        "entries",
        ["provider_id", "native_id"],
        unique=True,
        sqlite_where=sa.text("native_id IS NOT NULL"),
        postgresql_where=sa.text("native_id IS NOT NULL"),
    )
    op.create_index(
        "ix_entries_logged_at_id",
        "entries",
        [sa.text("logged_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_entries_ingested_at_id",
        "entries",
        [sa.text("ingested_at DESC"), sa.text("id DESC")],
        unique=False,
    )

    op.create_table(
        "opinions",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("work_id", UUID, nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("provider_item_id", AUTO, nullable=False),
        sa.Column("rating_raw", DECIMAL, nullable=True),
        sa.Column("rating_scale_id", sa.String(length=64), nullable=True),
        sa.Column("rating_normalized", sa.Integer(), nullable=True),
        sa.Column("subject_ref", JSON_COL, nullable=True),
        sa.Column("is_liked", sa.Boolean(), nullable=True),
        sa.Column("review_text", sa.Text(), nullable=True),
        sa.Column("review_format", sa.String(length=16), nullable=True),
        sa.Column("contains_spoilers", sa.Boolean(), nullable=True),
        sa.Column("authored_at", TS, nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("deleted_at", TS, nullable=True),
        sa.CheckConstraint(
            "review_format IN ('plain', 'markdown', 'html')",
            name=op.f("ck_opinions_review_format"),
        ),
        sa.CheckConstraint(
            "rating_normalized IS NULL OR (rating_normalized BETWEEN 0 AND 100)",
            name=op.f("ck_opinions_rating_normalized_range"),
        ),
        sa.CheckConstraint(
            "rating_raw IS NULL OR rating_scale_id IS NOT NULL",
            name=op.f("ck_opinions_rating_needs_scale"),
        ),
        sa.CheckConstraint(
            "review_text IS NULL OR review_format IS NOT NULL",
            name=op.f("ck_opinions_review_needs_format"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_item_id"],
            ["provider_items.id"],
            name=op.f("fk_opinions_provider_item_id_provider_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rating_scale_id"],
            ["rating_scales.id"],
            name=op.f("fk_opinions_rating_scale_id_rating_scales"),
        ),
        sa.ForeignKeyConstraint(
            ["work_id"], ["works.id"], name=op.f("fk_opinions_work_id_works"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opinions")),
        sa.UniqueConstraint(
            "provider_id", "provider_item_id", name="uq_opinions_provider_id_provider_item_id"
        ),
    )
    op.create_index(
        "ix_opinions_rating_normalized", "opinions", ["rating_normalized"], unique=False
    )
    op.create_index("ix_opinions_work_id", "opinions", ["work_id"], unique=False)

    op.create_table(
        "resolution_queue",
        sa.Column("id", AUTO, autoincrement=True, nullable=False),
        sa.Column("subject", sa.String(length=16), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("payload_ref", AUTO, nullable=True),
        sa.Column("candidates", JSON_COL, server_default=sa.text("'[]'"), nullable=False),
        sa.Column("proposed", JSON_COL, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("suggestion_kind", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "decision IN ('linked', 'created', 'split', 'ignored')",
            name=op.f("ck_resolution_queue_decision"),
        ),
        sa.CheckConstraint(
            "subject IN ('work', 'creator')",
            name=op.f("ck_resolution_queue_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["payload_ref"],
            ["provider_items.id"],
            name=op.f("fk_resolution_queue_payload_ref_provider_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resolution_queue")),
    )
    op.create_index(
        "ix_resolution_queue_decided_at", "resolution_queue", ["decided_at"], unique=False
    )

    # The 19th table, and the only one whose DDL is dialect-specific : an FTS5 virtual table
    # on SQLite, a tsvector column with a GIN index on Postgres. It cannot be expressed in the
    # shared MetaData, so the one implementation lives in aggregato.db.search.
    sync_create_search_index(op.get_bind())


def downgrade() -> None:
    # Forward-only is policy, not an omission (data-model.md §6, ). A downgrade that drops
    # 18 tables destroys the archive this application exists to keep, and the operator's actual
    # way back from a bad migration is the pre-migration file copy that aggregato.db.migrate takes.
    # Offering a green-looking `alembic downgrade` instead of that would be a trap.
    raise NotImplementedError("migrations are forward-only (data-model.md §6); restore the backup")
