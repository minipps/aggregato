# Data model

This document describes the current storage schema and the rules that protect archive data.
See [architecture.md](architecture.md) for process boundaries and [research.md](research.md) for
storage and portability decisions.

Types are written in portable terms. Concrete mappings: `uuid` → `sqlalchemy.Uuid`; `bigint pk` →
`BigInteger` with a SQLite `INTEGER` variant so it aliases rowid; `ts` → `DateTime(timezone=True)`
**always stored UTC**; `json` → `JSON` with a `JSONB` variant on Postgres; every `enum` is a text
column with a `CHECK` constraint, never a native database enum.

---

## 1. Closed vocabularies

Defined in `domain/enums.py`. Providers cannot extend these vocabularies; widening one is a core
change that requires a migration.

| Enum | Values |
|---|---|
| `media_type` | `film`, `tv`, `book`, `comic`, `manga`, `anime`, `album`, `track`, `game`, `other` |
| `media_family` | `screen`, `print`, `audio`, `interactive`, `other` |
| `entry_kind` | `watch`, `rewatch`, `listen`, `read`, `finish`, `progress`, `drop` |
| `logged_precision` | `exact`, `day`, `month`, `year`, `unknown` |
| `confidence` | `asserted`, `matched`, `manual` |
| `creator_kind` | `person`, `group`, `studio`, `imprint`, `unknown` |
| `alias_kind` | `primary`, `alias`, `romanization`, `native`, `credited_as` |
| `role` | `author`, `illustrator`, `translator`, `editor`, `director`, `writer`, `composer`, `performer`, `featured_performer`, `voice`, `narrator`, `studio`, `publisher`, `developer`, `other` |
| `review_format` | `plain`, `markdown`, `html` |
| `scale_kind` | `linear`, `ordinal` |
| `capability` | `poll`, `backfill`, `file_import`, `now_playing`, `reports_deletes`, `has_ratings`, `has_reviews`, `has_credits`, `scrapes`, `push` (`push` reserved, ignored in v1) |
| `acquisition` | `api`, `feed`, `export`, `scrape` |
| `error_class` | `auth`, `rate_limit`, `transport`, `parse`, `structure_changed`, `blocked`, `internal` |
| `run_status` | `running`, `success`, `partial`, `failed` |
| `provider_status` | `disabled`, `idle`, `syncing`, `degraded`, `misconfigured` |
| `resolution_subject` | `work`, `creator` |
| `resolution_decision` | `linked`, `created`, `split`, `ignored` |

**`media_type` → `media_family`** is a pure function in `domain/families.py`, not a stored column:
`screen` = film, tv, anime · `print` = book, comic, manga ·
`audio` = album, track · `interactive` = game · `other` = other.
API `media_family` filters expand to a `media_type IN (…)` predicate.

---

## 2. Works and logs

### `works`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | |
| `media_type` | enum | CHECK against the vocabulary |
| `title` | text not null | |
| `sort_title` | text not null | derived: article-stripped, casefolded |
| `original_title` | text null | |
| `release_year` | int null | |
| `parent_work_id` | uuid null → works.id | season → series, track → album |
| `sequence_number` | int null | season/track number |
| `image_url` | text null | platform payload only |
| `metadata` | json not null default `{}` | provider long tail |
| `created_at`, `updated_at` | ts not null | |

Indexes: `(media_type, sort_title, release_year)` for name matching; `(parent_work_id)`.
Validation: `parent_work_id` must not create a cycle; a work may not be its own parent.

### `external_ids`

| Column | Type | Notes |
|---|---|---|
| `id` | bigint pk | |
| `work_id` | uuid not null → works.id | |
| `namespace` | text not null | `tmdb`, `imdb`, `tvdb`, `isbn13`, `olid`, `mal`, `anilist`, `mbid_recording`, … |
| `value` | text not null | |
| `source` | text not null | provider id that asserted it |
| `confidence` | enum not null | |
| `created_at` | ts not null | |

Unique index `(namespace, value)` keeps an asserted work identifier attached to one work. A unique
constraint on `(namespace, value, work_id)` preserves the row key; an index on `(namespace, value)`
supports resolution lookup.
Validation: `namespace` from a registered set (extended by core, not by plugins); `value` trimmed,
non-empty; ISBN namespaces checksum-validated before insert.

### `provider_items`

| Column | Type | Notes |
|---|---|---|
| `id` | bigint pk | |
| `provider_id` | text not null | |
| `native_id` | text not null | |
| `work_id` | uuid null → works.id | null until resolved |
| `title_as_given` | text not null | |
| `raw_payload` | json not null | verbatim; the replay source |
| `schema_version` | int not null | last `normalize` version whose replacement committed for this item |
| `first_seen_at`, `last_seen_at` | ts not null | |

Unique `(provider_id, native_id)` is the provider-item idempotency key. Indexes on
`(provider_id, schema_version)` and `(work_id)` support replay and work lookups.

When a provider's `schema_version` changes, the worker replays retained payloads in count- and
byte-bounded batches before fetching. Each record's replacement and retirement of its old derived
rows share a savepoint. A normalization, validation, or write rejection preserves the old facts and
review search documents and item version, and records an ingest failure. Accepted batches commit
before the next child runs, so an interrupted replay resumes at the remaining stale items. An
incomplete replay does not proceed to fetch.

### `entries`

| Column | Type | Notes |
|---|---|---|
| `id` | bigint pk | |
| `work_id` | uuid not null → works.id | |
| `provider_id` | text not null | |
| `provider_item_id` | bigint not null → provider_items.id | |
| `native_id` | text null | null where the platform gives the event no id |
| `kind` | enum not null | |
| `logged_at` | ts not null | |
| `logged_precision` | enum not null | Required; no default is inferred |
| `subject_ref` | json null | fixed-key sub-unit (§3 below) |
| `subject_ref_key` | text null | canonical portable key for entries without a native id |
| `progress_value` | numeric null | how far through — distinct from `subject_ref` |
| `progress_unit` | text null | |
| `metadata` | json not null default `{}` | |
| `ingested_at` | ts not null | |
| `deleted_at` | ts null | soft delete / tombstone |

Unique `(provider_id, native_id)` where `native_id` is not null (partial index, both dialects).
Indexes: `(logged_at desc, id desc)` for the default keyset order; `(work_id)`; `(provider_id)`;
`(provider_item_id)`; `(ingested_at desc, id desc)`; and active-row indexes for logged-time and
ingestion-time ordering.
Validation: `progress_value` requires `progress_unit`; `logged_precision = exact` requires a time
component; a `progress` kind requires a progress value.

**Entries without a native id** use the unique fallback key
`(provider_item_id, kind, logged_at, subject_ref_key)` in a partial index for rows whose `native_id`
is null. The writer
canonicalizes `subject_ref` for this portable index and uses an upsert, so resyncs do not need a
read-before-write race or duplicate history.

### `opinions`

| Column | Type | Notes |
|---|---|---|
| `id` | bigint pk | |
| `work_id` | uuid not null → works.id | |
| `provider_id` | text not null | |
| `provider_item_id` | bigint not null → provider_items.id | |
| `rating_raw` | numeric null | |
| `rating_scale_id` | text null → rating_scales.id | |
| `rating_normalized` | int null | 0..100, derived |
| `subject_ref` | json null | |
| `is_liked` | bool null | |
| `review_text` | text null | |
| `review_format` | enum null | |
| `contains_spoilers` | bool null | |
| `authored_at` | ts null | |
| `updated_at` | ts not null | |
| `deleted_at` | ts null | |

Unique `(provider_id, provider_item_id)` — per-sub-unit ratings arrive as distinct `provider_items`,
so they do not collide. Indexes: `(rating_normalized)`, `(work_id)`, and
`(deleted_at, provider_item_id, rating_normalized desc)` for active score sorting.
Validation: `rating_raw` requires `rating_scale_id`; `rating_raw` must lie within that scale's bounds
and land on its step; `rating_normalized` must equal the recomputed value (asserted in tests, not by a
DB constraint); `review_text` requires `review_format`.

### `rating_scales`

Scale ids are global, immutable keys rather than provider-local names. Providers must choose
stable, namespaced ids; reusing an id with a different definition rejects the run before any
opinion is written. This keeps stored raw ratings recomputable and prevents two platforms from
silently assigning different meanings to the same `rating_scale_id`.

`id` text pk · `min_value` numeric · `max_value` numeric · `step` numeric · `kind` enum ·
`labels` json null (the explicit `value → normalized` map, required when `kind = ordinal`).
Validation: `max_value > min_value`; `step > 0`; ordinal scales must map every permitted value.

---

## 3. `subject_ref`

`subject_ref` is validated as a Pydantic model with `extra="forbid"` in the parent process at the
ingest boundary:

```json
{ "season": 2, "episode": 4 }
{ "disc": 1, "track": 7 }
```

Permitted keys: `season`, `episode`, `track`, `disc`, `chapter`, `volume`. Positive integers only.
`null` means the record pertains to the whole work — the overwhelmingly common case. Any other key,
any non-integer, or an empty object is an ingest failure for that record, not a silently dropped
field.

Aggregate queries filter `subject_ref IS NULL` unless `include_subunits=true`. A shared query builder
enforces this rule.

---

## 4. Creators and credits

### `creators`

`id` uuid pk · `kind` enum · `name` text (best-known display form) · `sort_name` text ·
`image_url` text null · `metadata` json · `created_at`, `updated_at` ts.

### `creator_aliases`

| Column | Type | Notes |
|---|---|---|
| `id` | bigint pk | |
| `creator_id` | uuid not null → creators.id | |
| `name` | text not null | as seen |
| `normalized` | text not null | casefolded, accent-stripped, punctuation-normalized |
| `media_family` | enum not null | the scope this name form is trusted in |
| `kind` | enum not null | |
| `source` | text not null | |

Unique `(creator_id, normalized, media_family)`. Index `(normalized, media_family)` supports name
resolution. One creator can hold aliases in several families after a cross-family merge.

### `creator_external_ids`

Mirrors `external_ids`: `id` bigint pk · `creator_id` uuid → creators.id · `namespace` text ·
`value` text · `source` text · `confidence` enum. Unique `(namespace, value)` and index
`(namespace, value)`. An asserted creator identifier is unscoped by media family.

### `work_credits`

| Column | Type | Notes |
|---|---|---|
| `id` | bigint pk | |
| `work_id` | uuid not null → works.id | |
| `creator_id` | uuid not null → creators.id | |
| `role` | enum not null | |
| `role_raw` | text null | the platform's own term, verbatim |
| `credited_as` | text null | name as this work credits them |
| `position` | int not null | 0 = first-billed; payload order where unexpressed |
| `source` | text not null | provider that asserted the credit |
| `link_confidence` | enum not null | how creator identity was established |
| `manual_from_creator_id` | uuid null | historical creator identity used to preserve a manual split |

Unique `(work_id, creator_id, role, source)`. Indexes `(creator_id, role)`, `(work_id)`.

`link_confidence` is required so the operator can distinguish name matches from asserted identifiers.
`manual_from_creator_id` is nullable historical provenance with no foreign key: it retains the
pre-split identity even if that creator is later merged or deleted. A split is matched on work,
provider source, position, `role_raw`, and `credited_as`. It applies during resync only when automatic
resolution, after following active creator-merge records, still reaches that historical identity.
The split target remains the credit's `creator_id`. Merges leave the historical origin unchanged;
creator merges reject a collision that would discard a distinct manual override. Undo restores the
saved rows only when current state still matches the operation's snapshot.

---

## 5. Resolution

### `resolution_queue`

`id` bigint pk · `subject` enum · `provider_id` text · `payload_ref` bigint null → provider_items.id ·
`candidates` json (ranked ids with the reason each was proposed, shown in the UI) ·
`proposed` json (what gets created if rejected) · `created_at` ts · `decided_at` ts null ·
`decision` enum null · `suggestion_kind` text null.

`suggestion_kind` distinguishes a genuine ambiguity from a proactive cross-family creator suggestion.
They are counted separately so queue depth remains useful as an ambiguity signal.

Index `(decided_at)` — the open-queue query is `WHERE decided_at IS NULL`.

### `merge_log`

`id` bigint pk · `subject` enum · `operation` text (`merge` | `split`) ·
`winner_id` uuid · `loser_ids` json · `moved_credit_ids` json null · `performed_at` ts ·
`undone_at` ts null · `snapshot` json.

`snapshot` stores the pre-operation rows needed to undo a merge, split, or resolution-queue decision.
Undo checks that affected rows have not changed since the operation; when a safe restore is no longer
possible, it rejects the request instead of overwriting later edits.

---

## 6. Providers, sync, and failures

### `providers`

`id` text pk · `enabled` bool · `status` enum · `acquisition` enum · `schema_version` int ·
`reviewed` bool · `config` json · `last_error` json null · `created_at`, `updated_at` ts.

`reviewed` is false for drop-in development providers, which the UI labels as unreviewed. `config`
contains database overrides only; file values remain file-pinned and are not copied into the table.
`acquisition` records whether data comes through an API, feed, export, or scrape; provider code must
follow the acquisition hierarchy and scraping policy in [CONTRIBUTING.md](../CONTRIBUTING.md).

### `settings`

`key` text pk · `value` json · `updated_at` ts. This table stores host-wide operator settings such
as retention options; provider-specific overrides remain in `providers.config`.

### `provider_state`

`provider_id` text pk → providers.id · `cursor` json null · `next_run_at` ts null ·
`effective_interval_seconds` int · `consecutive_failures` int · `retry_step` int ·
`last_success_at` ts null · `requested_mode` text null · `requested_lineage_id` uuid null ·
`last_window_item_count` int null · `last_failed_window_item_count` int null · `kv` json.

`cursor` stores the provider's opaque pagination checkpoint. `next_run_at` controls ordinary
scheduling: `NULL` means unscheduled, and the worker selects only rows with `next_run_at <= now`.
Enabling a provider or requesting a manual sync writes an explicit due time. A valid configuration
save also schedules an enabled, degraded provider whose ordinary schedule was suspended. A deployed
normalizer change alone does not reactivate a suspended history sync; request a sync after updating
it. Credential checks, imports, and failure replays are separately queued worker work and can be
admitted without an ordinary due time, including while disabled. `requested_mode` and
`requested_lineage_id` carry one-shot operator requests to the worker.

`last_window_item_count` is the accepted full-window baseline used by the sanity check;
`last_failed_window_item_count` is diagnostic and never becomes the next baseline. `kv` is a
reserved host mapping passed as input to now-playing contexts. Mutations to `ProviderContext.state`
are per-run scratch: the child protocol has no state writeback, so those mutations are not persisted.

Providers declaring `now_playing` also use this same row for transient current playback. These
columns are independent of the history schedule:

| Column | Type | Meaning |
|---|---|---|
| `now_playing_item` | json null | Latest parent-validated `NowPlayingItem`; never a log event |
| `now_playing_changed_at` | ts null | Host time when the normalized item last changed |
| `now_playing_checked_at` | ts null | Last completed now-playing attempt, used for freshness |
| `now_playing_next_poll_at` | ts null | Independent next poll; `NULL` suspends polling |
| `now_playing_failures` | int not null, default 0 | Position in the transient retry ladder |
| `now_playing_config_fingerprint` | text(64) null | Hash of resolved non-secret settings and provider schema version |

Success with an item or with no playback resets `now_playing_failures` and schedules the next check
15 seconds later. Equal normalized items update `now_playing_checked_at` but preserve
`now_playing_changed_at`. Any failed attempt clears the item immediately; transient failures use the
1m/5m/15m/1h ladder and honour a longer `Retry-After`, while auth, blocked, and structure-change
failures set `now_playing_next_poll_at` to `NULL` until configuration, schema, or enablement changes.
The API omits an item whose checked time is older than 45 seconds. No now-playing state is copied to
`provider_items`, `entries`, `opinions`, or `sync_runs`.

Ordinary scheduling uses `next_run_at <= now`; `NULL` is not treated as due. Index
`(next_run_at)` supports this lookup.

### Worker ownership and recovery

API and worker startup apply migrations. The scheduler worker holds a singleton lock for its lifetime
and recovers interrupted runs only after acquiring it. Recovery closes leftover running records,
releases providers left in `syncing`, and marks interrupted ordinary syncs due again without changing
the provider cursor. The next run starts from the last committed cursor; ingest upserts make
replaying records safe. The scheduler is designed to run as one worker per database.

### `sync_runs`

`id` bigint pk · `provider_id` text · `lineage_id` uuid · `attempt` int ·
`mode` text (`incremental` | `full` | `import` | `replay` | `check`) · `status` enum ·
`phase` enum (`starting` | `checking` | `replaying` | `fetching` | `ingesting` | `finalizing` |
`finished` | `failed`) · `started_at`, `finished_at`, `updated_at` ts ·
`items_seen`, `items_written`, `items_failed` int · `progress_total` int null (unknown for providers
that cannot declare a total) · `checkpoint_count` int · `last_checkpoint_at` ts null ·
`progress_revision` int ·
`error_class` enum null · `error_message` text null · `log_excerpt` text null · `log` text null ·
`raw_responses` json null · `cursor_before`, `cursor_after` json null.

The worker-side parent writes progress, phase, checkpoint metadata, and `updated_at` while a child
runs. The API queues work and reports stored state; it does not run syncs. `progress_total` is
nullable because a live acquisition may not know its eventual size. The sync status endpoint and
WebSocket read the durable run rows.

Read-only run summaries omit `error_message`, `log_excerpt`, and cursors. The operator-only
diagnostics endpoint returns the child log and, for each captured HTTP response, only its method,
sanitized URL, and integer status. It does not expose response bodies or headers.

Indexes `(provider_id, started_at desc)`, `(lineage_id)`.

### `import_jobs` and `replay_jobs`

These tables hold queued worker work outside the ordinary schedule. `import_jobs` records provider,
uploaded file path, creation and completion state, attempts, and lease ownership. `replay_jobs` links
an ingest failure to its provider and lineage, with claim, lease, completion, and error fields. The
worker claims and executes these jobs; the API only queues them.

### `ingest_failures`

`id` bigint pk · `provider_id` text · `sync_run_id` bigint → sync_runs.id ·
`native_id` text null · `raw_payload` json · `error` text · `stage` text
(`fetch` | `validate` | `normalize` | `write`) · `created_at` ts · `resolved_at` ts null.

The raw payload and captured provider item identity let an operator replay a corrected record.
Failure listing, including payloads, is operator-only.

### `sessions`

`id` text pk (signed opaque) · `created_at` ts · `expires_at` ts ·
`token_fingerprint` text — so rotating `api.token` invalidates every existing session.

### `image_cache`

`url_hash` text pk (sha256 of source URL) · `source_url` text ·
`bytes_sha256` text null · `content_type` text null · `size_bytes` int null · `fetched_at` ts null ·
`failed_at` ts null · `failure_count` int. Index `(bytes_sha256)` for byte-level deduplication.

### Search index

`search_index`: SQLite uses an FTS5 virtual table over `(kind, ref_id, text)`; PostgreSQL uses a
table with a `tsvector` column and a GIN index. The ingest writer updates it in the same transaction
as the described row. It contains work titles and opinion review text.

### Portable export

The operator-only export endpoint supports on-disk SQLite databases only; PostgreSQL export returns
an unavailable response. The archive contains the SQLite data, public provider configuration, and
cached images. It removes sessions, import and replay jobs, ingest failures and their raw payloads;
clears run diagnostics, pagination cursors, pending requests, and the next ordinary run time; clears
transient now-playing state and `provider_state.kv`; and disables every provider. Credentials are
replaced with public settings. The archive retains the user's history and stored provider-item
payloads, plus run summaries without diagnostics.

### Migrations

Alembic migrations are forward-only and run automatically at startup. Before applying a pending
migration to an existing on-disk SQLite database, startup writes a timestamped `.bak` file beside
the database. No backup is created when the database is already at the migration head. Backup
retention is managed manually; startup does not prune old `.bak` files. Alembic owns the version
table; there is no second migration mechanism.

Recent revisions:

- `0018` adds transient now-playing state to `provider_state`, including `now_playing_failures` with
  a zero default.
- `0019`, “Keep manual creator splits authoritative during ingestion resyncs,” adds the nullable
  historical split-origin column and backfills active split credits that still point at the live
  split destination.
- `0020`, “index entries by provider item,” adds the ordinary index on `entries.provider_item_id`.

The archive is single-user and has no `user_id` column.

#### Rebuilding a table on SQLite deletes its children

SQLite cannot `ALTER` a column or drop a constraint, so `render_as_batch=True` handles those by
rebuilding the table: create a copy, copy the rows, **`DROP` the original**, rename. `DROP TABLE`
performs an implicit `DELETE FROM` first, and that delete fires every `ON DELETE CASCADE` pointing
at the table. `engine.py` connects with `foreign_keys=ON`, so rebuilding `works` empties `entries`,
`opinions`, `work_credits`, and `external_ids`, and nulls `provider_items.work_id` — the migration
deletes the log it meant to alter. Alembic reports nothing wrong; the rebuild succeeds.

Any revision that alters a column or a constraint on a table other tables reference must therefore
turn foreign keys off around the rebuild:

```python
context = op.get_context()
with context.autocommit_block():
    op.execute("PRAGMA foreign_keys=OFF")
try:
    with op.batch_alter_table("works") as batch:
        batch.create_check_constraint("media_type", expression)
finally:
    with context.autocommit_block():
        op.execute("PRAGMA foreign_keys=ON")
```

`autocommit_block` is not optional: `PRAGMA foreign_keys` is silently ignored inside a transaction,
and env.py runs migrations in one. `PRAGMA defer_foreign_keys` is not a substitute — it defers
constraint *checking*, while the implicit delete still runs cascade *actions*.
`0005_merge_seasons_into_series.py` is the worked example.

#### Every data-touching revision lands with a survival test

A revision that rewrites or retypes rows gets a test that seeds rows **at the previous revision**,
upgrades to head, and asserts they are still there — not just that the DDL applied. Use
`command.upgrade(config, "0004")` to stop at a revision;
`tests/unit/test_migrations.py::test_0005_keeps_the_log_it_retypes` is the pattern. A test that only
upgrades an empty database proves nothing about cascade damage, because there is nothing to cascade.

#### An applied revision is never deleted or renumbered

Once a revision has run anywhere — including a development container — its id is written to
`alembic_version`. Deleting the file, renaming it, or rewriting its `down_revision` leaves that
database pointing at a revision that no longer exists, and startup dies with `Can't locate revision
identified by '…'` on every boot. Reverting a schema change means **a new revision forward**, never
editing history. See the recovery runbook in [operations.md](operations.md).

---

## 7. State transitions

### Sync run

```
                 ┌─────────► success   (cursor advances; retry_step := 0)
running ─────────┼─────────► partial   (cursor advances to last checkpoint; retry ladder continues)
                 └─────────► failed    (cursor unchanged)
```

The worker-side parent writes progress and closes the run. The cursor advances only to the last
checkpoint the child flushed. A `partial` run retains and writes records from before its failure; the
retry starts from that checkpoint. The operational phase moves through `starting` →
`checking`/`replaying`/`fetching` → `ingesting` → `finalizing`, then ends at `finished` or `failed`.
It is observability state, not a second outcome state.

### Provider status

```
disabled ──enable──► idle ──due──► syncing ──ok──► idle
                       ▲              │
                       │              ├─recoverable failure──► idle (next_run_at = ladder step)
                       │              │      consecutive_failures ≥ threshold ──► degraded
                       │              └─auth | blocked | structure_changed────► degraded (no retry)
                       │
              misconfigured ◄── invalid provider configuration at startup
```

Recoverable failures use the retry ladder; after the failure threshold, `degraded` retries no faster
than the normal interval. `auth`, `blocked`, and `structure_changed` set `next_run_at` to `NULL` and
wait for an explicit operator action. Provider status is shown in the UI, `/health`, and application
logs.

### Current playback

```
absent ──active result──► current ──equal result──► current (checked_at refreshed)
   ▲                         │  │
   │                         │  ├─changed result──► current (changed_at replaced)
   │                         │  ├─idle result─────► absent
   │                         │  └─failed attempt──► absent
   │                         └─checked_at older than 45s ──► omitted by API
   └─configuration/schema/enablement change reactivates a suspended poll
```

The monitor runs at most three isolated now-playing children concurrently. It polls successful
providers every 15 seconds, and the authenticated WebSocket reads durable rows every 500 ms and sends
only semantic snapshot changes. Multiple providers reporting the same work remain separate items.

### Retry ladder

`retry_step` 0→1→2→3 maps to 1m, 5m, 15m, 1h; beyond that, the normal interval. Reset to 0 on any
`success`. Rate limits can stretch `effective_interval_seconds` for the remainder of the session
and honor `Retry-After`.

### Resolution item

```
open ──operator──► linked | created | split | ignored     (decided_at set; reversible via merge_log)
```

### Entry lifecycle

```
absent ──ingest──► live ──provider reports delete──► tombstoned (deleted_at set)
                     └──inferred delete──► tombstoned   only after a successful `full` run
                                                        with a passing window sanity check,
                                                        infer_deletes enabled, and no record failures
                                                        for a provider without reports_deletes
```

Any fetch, normalization, validation, or write rejection skips inferred deletion for the run.
Tombstoned rows are excluded from every read unless `include_deleted=true`; they are never
hard-deleted.
