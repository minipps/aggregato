# Data model

[db/schema.py](../aggregato/db/schema.py) is authoritative for tables, columns, constraints, and indexes. Portable SQLAlchemy types are in [db/types.py](../aggregato/db/types.py); enum vocabularies are in [domain/enums.py](../aggregato/domain/enums.py). This guide records relationships and archive rules rather than duplicating the full schema.

The database is single-user and has no `user_id` column. Storage uses SQLAlchemy Core connections,
SQLite with WAL by default, or PostgreSQL. Timestamps are timezone-aware UTC; UUIDs and JSON use
the shared portable types.

## Schema overview

| Area | Tables | Relationships |
|---|---|---|
| Catalog | works, external_ids, provider_items | Works may have parent works. External IDs belong to one work. Each provider item stores its raw payload and may link to a work. |
| History | entries, opinions, rating_scales | Entries and opinions link to both a work and a provider item. Opinions may reference a rating scale. |
| Credits | creators, creator_aliases, creator_external_ids, work_credits | Credits join creators to works and retain the source role and identity confidence. |
| Resolution | resolution_queue, merge_log | Queue items may point to provider items; merge-log snapshots support guarded undo. |
| Sync | providers, provider_state, sync_runs, import_jobs, replay_jobs, ingest_failures | Provider state owns the ordinary schedule and cursor. Runs and queued jobs belong to the worker; failures retain rejected payloads. |
| Host data | settings, sessions, image_cache, search_index | Operator settings, login sessions, cached images, and the dialect-specific search projection. |

## Archive rules

- Provider and domain vocabularies are closed. Enum values are text columns with CHECK constraints. Parent-side subject-reference validation allows only positive season, episode, track, disc, chapter, and volume numbers; unknown keys and empty objects become ingest failures, while null means a whole-work record.
- A provider item is unique by provider and native item id. Entries with a platform event id are unique by provider and event id. Entries without one use the fallback key of provider item, kind, logged time, and canonical subject reference. Writers use unique constraints with upserts, never a read-before-write deduplication check.
- provider_items.raw_payload is the source for normalization replay. Bump schema_version when normalization changes retained payload mapping. The worker replays stale items in count- and byte-bounded batches before fetching; an incomplete replay blocks fetch. A rejected replacement preserves the previous facts and item version.
- The parent validates each record in a savepoint. A rejected record becomes an ingest failure while later records continue. The cursor advances only to a checkpoint flushed by the child; partial runs retain accepted records and resume from the last committed cursor.
- Log facts use deleted_at tombstones. Inferred tombstones require a successful full run, the operator's infer_deletes opt-in, a provider without reports_deletes, a passing window sanity check, and no record failures.
- Work external IDs are globally unique by namespace and value. Work resolution prefers asserted IDs; title matching is used only when it has one candidate, while ambiguity is retained for review. Creator aliases are scoped by media family; asserted creator IDs are global. Keep role_raw and credit identity confidence so normalization and manual splits can survive resyncs.
- Merge and split operations retain snapshots. Undo must reject if affected rows changed since the operation; ingestion must preserve explicit manual creator decisions.
- Ratings retain the raw value and immutable scale definition; the normalized 0–100 value is derived. Ordinal scales require an explicit map, and normalized values are comparable within a scale only.
- Aggregate log queries exclude sub-unit records and tombstones by default. Include sub-units explicitly. Paged reads use keyset cursors containing the sort value and id; never use OFFSET. Invalid cursors are a 400, and null sort values come last in either direction.
- Search documents are updated in the same transaction as their source rows. SQLite uses FTS5; PostgreSQL uses tsvector with a GIN index.

Manual creator splits retain a historical creator UUID without a foreign key: the original creator
may later be merged and removed. The scoped credit override and merge snapshots must survive
source/target merges, undo, and resync. Work-row locks serialize PostgreSQL ingestion against splits.

## Scheduling state

`provider_state.next_run_at = NULL` means unscheduled. Enablement and manual sync make a provider
due; a valid configuration change reactivates an enabled, degraded provider with a suspended
schedule. Schema changes alone do not reactivate ordinary history syncs.

Recoverable failures use the 1-minute, 5-minute, 15-minute, and 1-hour retry ladder. Authentication,
blocking, and structural failures suspend automatic retries. Explicit checks and queued import or
replay jobs can run while a provider is disabled. Checks do not automatically retry or change
ordinary sync health. Disabling during a run prevents future scheduled fetches while the active
operation finishes. Run closure and cursor/retry updates share a transaction; stale completion
cannot overwrite a closed run's schedule.

Current playback is separate transient state, at most one item per provider. An idle or failed
attempt clears it; auth, blocked, and structural failures suspend its polling until configuration,
schema, or enablement changes. It never creates history or participates in identity resolution.

## Migrations

Alembic is the only migration history. API and worker startup apply pending revisions and back up an
existing on-disk SQLite database before migrating. Revisions are forward-only: never delete,
renumber, or edit an applied revision; make a new revision instead.

A migration that changes stored rows needs a survival test that seeds data at the previous revision, upgrades to the new revision, and checks that data remains. SQLite batch table rebuilds can fire foreign-key cascades when dropping the original table. When rebuilding a referenced table, disable foreign keys outside the transaction with Alembic's autocommit block, then restore them; deferred checks do not prevent cascade actions.
