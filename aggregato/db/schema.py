"""Table definitions (data-model.md).

SQLAlchemy Core rather than the ORM (research.md R3): the workload is bulk upsert and analytical
filtering, so an identity map and lazy loading are liabilities at 50,000 entries/hour, and the hot
paths are already written as set operations.

Every type choice that differs between SQLite and Postgres lives in ``types.py``; every enum column
is text plus a ``CHECK`` generated from the vocabulary itself. **There is no ``user_id`` column
anywhere** (FR-006) — adding one is a v2 schema break, accepted knowingly.

Timestamps carry no database or Python default on purpose. The caller passes them from the injected
clock, so tests can control time (Constitution II) and a row's ``ingested_at`` cannot silently
disagree with the run that wrote it.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from aggregato.db.types import AUTO_FK, AUTO_PK, DECIMAL, JSON_COL, TIMESTAMP, UUID_PK
from aggregato.domain.enums import (
    Acquisition,
    AliasKind,
    Confidence,
    CreatorKind,
    EntryKind,
    ErrorClass,
    IngestStage,
    LoggedPrecision,
    MediaType,
    ProviderStatus,
    ResolutionDecision,
    ResolutionSubject,
    ReviewFormat,
    Role,
    RunStatus,
    ScaleKind,
    check_constraint,
)

# Predictable constraint and index names, which is what makes Alembic's SQLite batch mode able to
# rebuild a table and put everything back.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


# --- Works and logs ---------------------------------------------------------------------------

works = Table(
    "works",
    metadata,
    Column("id", UUID_PK, primary_key=True),
    Column("media_type", String(32), nullable=False),
    Column("title", Text, nullable=False),
    # Derived: article-stripped and casefolded. Stored rather than computed because it is both the
    # sort key and half of the name-matching index, and neither dialect can index an expression
    # identically.
    Column("sort_title", Text, nullable=False),
    Column("original_title", Text),
    Column("release_year", Integer),
    # season -> series, track -> album. Self-referential; the writer rejects cycles.
    Column("parent_work_id", UUID_PK, ForeignKey("works.id", ondelete="SET NULL")),
    Column("sequence_number", Integer),
    # Platform payload only, never a URL we invented (FR-010, FR-033).
    Column("image_url", Text),
    Column("metadata", JSON_COL, nullable=False, server_default=text("'{}'")),
    Column("created_at", TIMESTAMP, nullable=False),
    Column("updated_at", TIMESTAMP, nullable=False),
    check_constraint("media_type", MediaType),
    # The name-matching lookup (FR-011): media type scopes it, sort_title selects,
    # release_year disambiguates.
    Index(
        "ix_works_media_type_sort_title_release_year", "media_type", "sort_title", "release_year"
    ),
    Index("ix_works_parent_work_id", "parent_work_id"),
)

external_ids = Table(
    "external_ids",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("work_id", UUID_PK, ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
    # tmdb, imdb, tvdb, isbn13, olid, mal, anilist, mbid_recording, ... Extended by core, never by
    # a plugin.
    Column("namespace", String(64), nullable=False),
    Column("value", Text, nullable=False),
    Column("source", String(64), nullable=False),
    Column("confidence", String(16), nullable=False),
    Column("created_at", TIMESTAMP, nullable=False),
    UniqueConstraint("namespace", "value", "work_id", name="uq_external_ids_namespace_value_work"),
    check_constraint("confidence", Confidence),
    # The resolution lookup (FR-009) — the cheapest and most reliable way two providers agree.
    Index("ix_external_ids_namespace_value", "namespace", "value"),
    Index("ix_external_ids_work_id", "work_id"),
)

provider_items = Table(
    "provider_items",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("provider_id", String(64), nullable=False),
    Column("native_id", Text, nullable=False),
    Column("work_id", UUID_PK, ForeignKey("works.id", ondelete="SET NULL")),
    Column("title_as_given", Text, nullable=False),
    # Verbatim. This is the replay source (FR-002): because normalize is pure, a corrected plugin
    # re-derives everything from here with no network.
    Column("raw_payload", JSON_COL, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("first_seen_at", TIMESTAMP, nullable=False),
    Column("last_seen_at", TIMESTAMP, nullable=False),
    # The idempotency key (FR-005). A resync writes nothing new because of this one constraint.
    UniqueConstraint("provider_id", "native_id", name="uq_provider_items_provider_id_native_id"),
    # How replay finds stale rows (research.md R16).
    Index("ix_provider_items_provider_id_schema_version", "provider_id", "schema_version"),
    Index("ix_provider_items_work_id", "work_id"),
)

entries = Table(
    "entries",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("work_id", UUID_PK, ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
    Column("provider_id", String(64), nullable=False),
    Column(
        "provider_item_id",
        AUTO_FK,
        ForeignKey("provider_items.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Null where the platform gives the event no id of its own. Those are deduplicated by the
    # writer on (provider_item_id, kind, logged_at, subject_ref) instead.
    Column("native_id", Text),
    Column("kind", String(16), nullable=False),
    Column("logged_at", TIMESTAMP, nullable=False),
    # Required, no default: a default would silently fabricate exactness (FR-004).
    Column("logged_precision", String(16), nullable=False),
    Column("subject_ref", JSON_COL),
    # How far through — deliberately distinct from subject_ref, which says *which* sub-unit.
    Column("progress_value", DECIMAL),
    Column("progress_unit", String(32)),
    Column("metadata", JSON_COL, nullable=False, server_default=text("'{}'")),
    Column("ingested_at", TIMESTAMP, nullable=False),
    # Soft delete. Tombstoned rows are never hard-deleted (FR-024).
    Column("deleted_at", TIMESTAMP),
    check_constraint("kind", EntryKind),
    check_constraint("logged_precision", LoggedPrecision),
    # "40" of what? A progress value without its unit is unreadable, so the pairing is a database
    # constraint rather than a writer convention.
    CheckConstraint(
        "(progress_value IS NULL) = (progress_unit IS NULL)",
        name="progress_value_needs_unit",
    ),
    # The idempotency key for events the platform *does* identify. Partial, because a null
    # native_id must not collide with another null — both dialects support this.
    Index(
        "uq_entries_provider_id_native_id",
        "provider_id",
        "native_id",
        unique=True,
        sqlite_where=text("native_id IS NOT NULL"),
        postgresql_where=text("native_id IS NOT NULL"),
    ),
    # The default keyset order (research.md R6). id breaks ties so the order is total, and no row
    # is skipped or repeated when timestamps collide — which they will, in bulk-imported history.
    Index("ix_entries_logged_at_id", text("logged_at DESC"), text("id DESC")),
    Index("ix_entries_work_id", "work_id"),
    Index("ix_entries_provider_id", "provider_id"),
    Index("ix_entries_ingested_at_id", text("ingested_at DESC"), text("id DESC")),
    # The common log path excludes tombstones and sub-units before applying either keyset sort.
    # These composite indexes keep that predicate and each permitted entry sort on one index walk.
    Index(
        "ix_entries_active_logged_at_id",
        "deleted_at",
        "subject_ref",
        text("logged_at DESC"),
        text("id DESC"),
    ),
    Index(
        "ix_entries_active_ingested_at_id",
        "deleted_at",
        "subject_ref",
        text("ingested_at DESC"),
        text("id DESC"),
    ),
)

opinions = Table(
    "opinions",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("work_id", UUID_PK, ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
    Column("provider_id", String(64), nullable=False),
    Column(
        "provider_item_id",
        AUTO_FK,
        ForeignKey("provider_items.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("rating_raw", DECIMAL),
    Column("rating_scale_id", String(64), ForeignKey("rating_scales.id")),
    # Derived from rating_raw and the scale (research.md R7). Recomputable, so a corrected scale
    # definition is repaired by replay rather than by re-syncing every platform.
    Column("rating_normalized", Integer),
    Column("subject_ref", JSON_COL),
    Column("is_liked", Boolean),
    Column("review_text", Text),
    Column("review_format", String(16)),
    Column("contains_spoilers", Boolean),
    Column("authored_at", TIMESTAMP),
    Column("updated_at", TIMESTAMP, nullable=False),
    Column("deleted_at", TIMESTAMP),
    # Per-sub-unit ratings arrive as distinct provider_items, so they do not collide here.
    UniqueConstraint(
        "provider_id", "provider_item_id", name="uq_opinions_provider_id_provider_item_id"
    ),
    check_constraint("review_format", ReviewFormat),
    # A raw rating means nothing without the scale it came from (FR-003).
    CheckConstraint(
        "rating_raw IS NULL OR rating_scale_id IS NOT NULL",
        name="rating_needs_scale",
    ),
    CheckConstraint(
        "review_text IS NULL OR review_format IS NOT NULL",
        name="review_needs_format",
    ),
    CheckConstraint(
        "rating_normalized IS NULL OR (rating_normalized BETWEEN 0 AND 100)",
        name="rating_normalized_range",
    ),
    Index("ix_opinions_rating_normalized", "rating_normalized"),
    Index("ix_opinions_work_id", "work_id"),
    # ``opinion_facts`` groups a live provider item before entries can sort by its score.
    Index(
        "ix_opinions_active_provider_item_rating",
        "deleted_at",
        "provider_item_id",
        text("rating_normalized DESC"),
    ),
)

rating_scales = Table(
    "rating_scales",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("min_value", DECIMAL, nullable=False),
    Column("max_value", DECIMAL, nullable=False),
    Column("step", DECIMAL, nullable=False),
    Column("kind", String(16), nullable=False),
    # The explicit value -> normalized map. Required when kind is ordinal, because interpolating
    # between labels invents precision the platform never had.
    Column("labels", JSON_COL),
    check_constraint("kind", ScaleKind),
)


# --- Creators and credits ---------------------------------------------------------------------

creators = Table(
    "creators",
    metadata,
    Column("id", UUID_PK, primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("name", Text, nullable=False),
    Column("sort_name", Text, nullable=False),
    Column("image_url", Text),
    Column("metadata", JSON_COL, nullable=False, server_default=text("'{}'")),
    Column("created_at", TIMESTAMP, nullable=False),
    Column("updated_at", TIMESTAMP, nullable=False),
    check_constraint("kind", CreatorKind),
    Index("ix_creators_sort_name", "sort_name"),
)

creator_aliases = Table(
    "creator_aliases",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("creator_id", UUID_PK, ForeignKey("creators.id", ondelete="CASCADE"), nullable=False),
    Column("name", Text, nullable=False),
    Column("normalized", Text, nullable=False),
    # The scope this name form is trusted in (FR-015). One creator legitimately holds aliases in
    # several families — that is exactly what a cross-family merge produces.
    Column("media_family", String(16), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("source", String(64), nullable=False),
    UniqueConstraint(
        "creator_id", "normalized", "media_family", name="uq_creator_aliases_creator_normalized"
    ),
    check_constraint("kind", AliasKind),
    # The hot lookup (research.md R8): one batched SELECT per run, never one per credit.
    Index("ix_creator_aliases_normalized_media_family", "normalized", "media_family"),
)

creator_external_ids = Table(
    "creator_external_ids",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("creator_id", UUID_PK, ForeignKey("creators.id", ondelete="CASCADE"), nullable=False),
    Column("namespace", String(64), nullable=False),
    Column("value", Text, nullable=False),
    Column("source", String(64), nullable=False),
    Column("confidence", String(16), nullable=False),
    UniqueConstraint(
        "namespace", "value", "creator_id", name="uq_creator_external_ids_namespace_value_creator"
    ),
    check_constraint("confidence", Confidence),
    # Deliberately **unscoped by family**, unlike aliases: an asserted identifier is identity
    # everywhere (FR-009 against FR-015).
    Index("ix_creator_external_ids_namespace_value", "namespace", "value"),
)

work_credits = Table(
    "work_credits",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("work_id", UUID_PK, ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
    Column("creator_id", UUID_PK, ForeignKey("creators.id", ondelete="CASCADE"), nullable=False),
    Column("role", String(32), nullable=False),
    # The platform's own term, verbatim, always — even when `role` maps cleanly (FR-016). This is
    # what replay re-derives from when the role vocabulary widens.
    Column("role_raw", Text),
    Column("credited_as", Text),
    # 0 = first-billed; payload order where the platform does not express billing.
    Column("position", Integer, nullable=False),
    Column("source", String(64), nullable=False),
    # How this creator's identity was established. Required by FR-013: a split must show the
    # operator which credits were joined by name rather than by an asserted identifier, and per
    # credit is the only place that information survives.
    Column("link_confidence", String(16), nullable=False),
    UniqueConstraint(
        "work_id", "creator_id", "role", "source", name="uq_work_credits_work_creator_role_source"
    ),
    check_constraint("role", Role),
    check_constraint("link_confidence", Confidence),
    Index("ix_work_credits_creator_id_role", "creator_id", "role"),
    Index("ix_work_credits_work_id", "work_id"),
)


# --- Resolution ------------------------------------------------------------------------------

resolution_queue = Table(
    "resolution_queue",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("subject", String(16), nullable=False),
    Column("provider_id", String(64), nullable=False),
    Column("payload_ref", AUTO_FK, ForeignKey("provider_items.id", ondelete="CASCADE")),
    # Ranked candidates, each carrying **the reason it was proposed** — shown in the UI, because a
    # candidate list without reasons is not a decision an operator can make (US4 scenario 1).
    Column("candidates", JSON_COL, nullable=False, server_default=text("'[]'")),
    Column("proposed", JSON_COL, nullable=False, server_default=text("'{}'")),
    Column("created_at", TIMESTAMP, nullable=False),
    Column("decided_at", TIMESTAMP),
    Column("decision", String(16)),
    # Distinguishes a genuine ambiguity from a proactive cross-family creator suggestion (FR-015).
    # The two must not be counted together, or queue depth stops being a risk signal (SC-002).
    Column("suggestion_kind", String(32)),
    check_constraint("subject", ResolutionSubject),
    check_constraint("decision", ResolutionDecision),
    # The open-queue query is WHERE decided_at IS NULL.
    Index("ix_resolution_queue_decided_at", "decided_at"),
)

merge_log = Table(
    "merge_log",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("subject", String(16), nullable=False),
    Column("operation", String(16), nullable=False),
    Column("winner_id", UUID_PK, nullable=False),
    Column("loser_ids", JSON_COL, nullable=False),
    Column("moved_credit_ids", JSON_COL),
    Column("performed_at", TIMESTAMP, nullable=False),
    Column("undone_at", TIMESTAMP),
    # The pre-operation row state. This is what makes undo possible (FR-017) without event-sourcing
    # the whole database.
    Column("snapshot", JSON_COL, nullable=False),
    check_constraint("subject", ResolutionSubject),
    Index("ix_merge_log_performed_at", text("performed_at DESC")),
)


# --- Providers, sync, and failures -------------------------------------------------------------

providers = Table(
    "providers",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("enabled", Boolean, nullable=False, server_default=text("0")),
    Column("status", String(16), nullable=False),
    Column("acquisition", String(16), nullable=False),
    Column("schema_version", Integer, nullable=False),
    # False for drop-in development providers, which the UI labels `unreviewed` (FR-041).
    Column("reviewed", Boolean, nullable=False, server_default=text("1")),
    # Database overrides only. File values are not copied here, so "file-pinned" stays answerable
    # (research.md R15).
    Column("config", JSON_COL, nullable=False, server_default=text("'{}'")),
    Column("last_error", JSON_COL),
    Column("created_at", TIMESTAMP, nullable=False),
    Column("updated_at", TIMESTAMP, nullable=False),
    check_constraint("status", ProviderStatus),
    check_constraint("acquisition", Acquisition),
)

provider_state = Table(
    "provider_state",
    metadata,
    Column(
        "provider_id", String(64), ForeignKey("providers.id", ondelete="CASCADE"), primary_key=True
    ),
    # Opaque and provider-owned. The host persists it and never interprets it.
    Column("cursor", JSON_COL),
    # The schedule *is* this column (research.md R1). No cron expression, no second store.
    Column("next_run_at", TIMESTAMP),
    Column("effective_interval_seconds", Integer, nullable=False),
    Column("consecutive_failures", Integer, nullable=False, server_default=text("0")),
    Column("retry_step", Integer, nullable=False, server_default=text("0")),
    Column("last_success_at", TIMESTAMP),
    # The sanity baseline (research.md R21): the previous run's item count for the same window.
    # This is the guard that makes a broken scraper look like a broken scraper rather than like an
    # emptied history.
    Column("last_window_item_count", Integer),
    # The provider's own opaque key/value store (FR-037).
    Column("kv", JSON_COL, nullable=False, server_default=text("'{}'")),
    # The only thing the scheduler selects on.
    Index("ix_provider_state_next_run_at", "next_run_at"),
)

sync_runs = Table(
    "sync_runs",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("provider_id", String(64), nullable=False),
    # Groups the retries of one logical piece of work (FR-019), so the UI can show four attempts as
    # one failing sync rather than four unrelated failures.
    Column("lineage_id", UUID_PK, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("mode", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_at", TIMESTAMP, nullable=False),
    Column("finished_at", TIMESTAMP),
    Column("items_seen", Integer, nullable=False, server_default=text("0")),
    Column("items_written", Integer, nullable=False, server_default=text("0")),
    Column("items_failed", Integer, nullable=False, server_default=text("0")),
    Column("error_class", String(32)),
    Column("error_message", Text),
    # Application logs, not user log data — the spec is explicit about the naming collision.
    Column("log_excerpt", Text),
    Column("cursor_before", JSON_COL),
    Column("cursor_after", JSON_COL),
    check_constraint("status", RunStatus),
    check_constraint("error_class", ErrorClass),
    Index("ix_sync_runs_provider_id_started_at", "provider_id", text("started_at DESC")),
    Index("ix_sync_runs_lineage_id", "lineage_id"),
)

ingest_failures = Table(
    "ingest_failures",
    metadata,
    Column("id", AUTO_PK, primary_key=True, autoincrement=True),
    Column("provider_id", String(64), nullable=False),
    Column("sync_run_id", AUTO_FK, ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False),
    # Stored so a fixed plugin can replay it (FR-023). Without the payload, "one bad record" is an
    # unreproducible bug report.
    Column("raw_payload", JSON_COL, nullable=False),
    Column("error", Text, nullable=False),
    Column("stage", String(16), nullable=False),
    Column("created_at", TIMESTAMP, nullable=False),
    Column("resolved_at", TIMESTAMP),
    check_constraint("stage", IngestStage),
    Index("ix_ingest_failures_provider_id_created_at", "provider_id", text("created_at DESC")),
    Index("ix_ingest_failures_resolved_at", "resolved_at"),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("created_at", TIMESTAMP, nullable=False),
    Column("expires_at", TIMESTAMP, nullable=False),
    # So rotating api.token invalidates every existing session (research.md R12). Sessions live in
    # the database precisely so rotation can actually reach them.
    Column("token_fingerprint", String(64), nullable=False),
    Index("ix_sessions_expires_at", "expires_at"),
)

image_cache = Table(
    "image_cache",
    metadata,
    # sha256 of the source URL: keying the endpoint this way avoids leaking source URLs into the UI.
    Column("url_hash", String(64), primary_key=True),
    Column("source_url", Text, nullable=False),
    # sha256 of the *bytes*, which deduplicates the same artwork arriving from several providers.
    Column("bytes_sha256", String(64)),
    Column("content_type", String(128)),
    Column("size_bytes", Integer),
    Column("fetched_at", TIMESTAMP),
    Column("failed_at", TIMESTAMP),
    Column("failure_count", Integer, nullable=False, server_default=text("0")),
    Index("ix_image_cache_bytes_sha256", "bytes_sha256"),
)
