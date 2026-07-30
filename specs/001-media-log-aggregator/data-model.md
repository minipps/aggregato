# Phase 1 Data Model: Aggregato

**Date**: 2026-07-29 | **Plan**: [plan.md](./plan.md) | **Portability rules**: [research.md](./research.md) R4

Types are written in portable terms. Concrete mappings: `uuid` → `sqlalchemy.Uuid`; `bigint pk` →
`BigInteger` with a SQLite `INTEGER` variant so it aliases rowid; `ts` → `DateTime(timezone=True)`
**always stored UTC**; `json` → `JSON` with a `JSONB` variant on Postgres; every `enum` is a text
column with a `CHECK` constraint, never a native database enum.

---

## 1. Closed vocabularies

Defined in `domain/enums.py`. A provider cannot extend any of them (FR-008); widening one is a core
change with a migration.

| Enum | Values |
|---|---|
| `media_type` | `film`, `tv_series`, `tv_season`, `book`, `comic`, `manga`, `anime_series`, `anime_season`, `album`, `track`, `game`, `podcast`, `podcast_episode`, `other` |
| `media_family` | `screen`, `print`, `audio`, `interactive`, `other` |
| `entry_kind` | `watch`, `rewatch`, `listen`, `read`, `finish`, `progress`, `drop` |
| `logged_precision` | `exact`, `day`, `month`, `year`, `unknown` |
| `confidence` | `asserted`, `matched`, `manual` |
| `creator_kind` | `person`, `group`, `studio`, `imprint`, `unknown` |
| `alias_kind` | `primary`, `alias`, `romanization`, `native`, `credited_as` |
| `role` | `author`, `illustrator`, `translator`, `editor`, `director`, `writer`, `composer`, `performer`, `featured_performer`, `voice`, `narrator`, `studio`, `publisher`, `developer`, `other` |
| `review_format` | `plain`, `markdown`, `html` |
| `scale_kind` | `linear`, `ordinal` |
| `capability` | `poll`, `backfill`, `file_import`, `reports_deletes`, `has_ratings`, `has_reviews`, `has_credits`, `scrapes`, `push` (`push` reserved, ignored in v1) |
| `acquisition` | `api`, `feed`, `export`, `scrape` |
| `error_class` | `auth`, `rate_limit`, `transport`, `parse`, `structure_changed`, `blocked`, `internal` |
| `run_status` | `running`, `success`, `partial`, `failed` |
| `provider_status` | `disabled`, `idle`, `syncing`, `degraded`, `misconfigured` |
| `resolution_subject` | `work`, `creator` |
| `resolution_decision` | `linked`, `created`, `split`, `ignored` |

**`media_type` → `media_family`** is a pure function in `domain/families.py`, not a stored column:
`screen` = film, tv_series, tv_season, anime_series, anime_season · `print` = book, comic, manga ·
`audio` = album, track, podcast, podcast_episode · `interactive` = game · `other` = other.
API `media_family` filters expand to a `media_type IN (…)` predicate (FR-029).

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
| `image_url` | text null | platform payload only (FR-010, FR-033) |
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

Unique `(namespace, value, work_id)`. Index `(namespace, value)` — the resolution lookup (FR-009).
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
| `raw_payload` | json not null | verbatim; the replay source (FR-002) |
| `schema_version` | int not null | which `normalize` version produced current derived rows |
| `first_seen_at`, `last_seen_at` | ts not null | |

Unique `(provider_id, native_id)` — the idempotency key (FR-005). Index `(provider_id, schema_version)`
so replay can find stale rows (R16). Index `(work_id)`.

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
| `logged_precision` | enum not null | FR-004 |
| `subject_ref` | json null | fixed-key sub-unit (§3 below) |
| `progress_value` | numeric null | how far through — distinct from `subject_ref` |
| `progress_unit` | text null | |
| `metadata` | json not null default `{}` | |
| `ingested_at` | ts not null | |
| `deleted_at` | ts null | soft delete / tombstone |

Unique `(provider_id, native_id)` **where `native_id` is not null** (partial index, both dialects).
Indexes: `(logged_at desc, id desc)` — the default keyset order (R6); `(work_id)`; `(provider_id)`;
`(ingested_at desc, id desc)` and a score-sort index for the other permitted `sort` values.
Validation: `progress_value` requires `progress_unit`; `logged_precision = exact` requires a time
component; a `progress` kind requires a progress value.

**Entries without a native id** cannot be deduplicated by key, so an entry whose provider supplies no
event id is deduplicated on `(provider_item_id, kind, logged_at, subject_ref)` by the writer instead.

### `opinions`

| Column | Type | Notes |
|---|---|---|
| `id` | bigint pk | |
| `work_id` | uuid not null → works.id | |
| `provider_id` | text not null | |
| `provider_item_id` | bigint not null → provider_items.id | |
| `rating_raw` | numeric null | |
| `rating_scale_id` | text null → rating_scales.id | |
| `rating_normalized` | int null | 0..100, derived (R7) |
| `subject_ref` | json null | |
| `is_liked` | bool null | |
| `review_text` | text null | |
| `review_format` | enum null | |
| `contains_spoilers` | bool null | |
| `authored_at` | ts null | |
| `updated_at` | ts not null | |
| `deleted_at` | ts null | |

Unique `(provider_id, provider_item_id)` — per-sub-unit ratings arrive as distinct `provider_items`,
so they do not collide. Indexes: `(rating_normalized)`, `(work_id)`.
Validation: `rating_raw` requires `rating_scale_id`; `rating_raw` must lie within that scale's bounds
and land on its step; `rating_normalized` must equal the recomputed value (asserted in tests, not by a
DB constraint); `review_text` requires `review_format`.

### `rating_scales`

`id` text pk · `min_value` numeric · `max_value` numeric · `step` numeric · `kind` enum ·
`labels` json null (the explicit `value → normalized` map, required when `kind = ordinal`).
Validation: `max_value > min_value`; `step > 0`; ordinal scales must map every permitted value.

---

## 3. `subject_ref`

A Pydantic model, `extra="forbid"`, validated **in the parent process** at the ingest boundary
(R17, FR-008):

```json
{ "season": 2, "episode": 4 }
{ "disc": 1, "track": 7 }
```

Permitted keys: `season`, `episode`, `track`, `disc`, `chapter`, `volume`. Positive integers only.
`null` means the record pertains to the whole work — the overwhelmingly common case. Any other key,
any non-integer, or an empty object is an ingest failure for that record, not a silently dropped
field.

**Aggregate rule (FR-007)**: every aggregate query filters `subject_ref IS NULL` unless
`include_subunits=true`. Enforced in one shared query builder with a named test (R18).

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
| `media_family` | enum not null | the scope this name form is trusted in (FR-015) |
| `kind` | enum not null | |
| `source` | text not null | |

Unique `(creator_id, normalized, media_family)`. Index `(normalized, media_family)` — the hot lookup
(R8). One creator legitimately holds aliases in several families; that is what a cross-family merge
produces.

### `creator_external_ids`

Mirrors `external_ids`: `id` bigint pk · `creator_id` uuid → creators.id · `namespace` text ·
`value` text · `source` text · `confidence` enum. Unique `(namespace, value, creator_id)`,
index `(namespace, value)`. **Unscoped by family** — an asserted identifier is identity everywhere
(FR-009 vs FR-015).

### `work_credits`

| Column | Type | Notes |
|---|---|---|
| `id` | bigint pk | |
| `work_id` | uuid not null → works.id | |
| `creator_id` | uuid not null → creators.id | |
| `role` | enum not null | |
| `role_raw` | text null | the platform's own term, verbatim (FR-016) |
| `credited_as` | text null | name as this work credits them |
| `position` | int not null | 0 = first-billed; payload order where unexpressed |
| `source` | text not null | provider that asserted the credit |
| `link_confidence` | enum not null | **added**: how creator identity was established |

Unique `(work_id, creator_id, role, source)`. Indexes `(creator_id, role)`, `(work_id)`.

`link_confidence` is not in the source design's table but is required by FR-013: a split must show the
operator which credits were joined by name matching rather than by an asserted identifier. Storing it
per credit is the only place that information survives.

---

## 5. Resolution

### `resolution_queue`

`id` bigint pk · `subject` enum · `provider_id` text · `payload_ref` bigint null → provider_items.id ·
`candidates` json (ranked ids **with the reason each was proposed** — FR shown in the UI) ·
`proposed` json (what gets created if rejected) · `created_at` ts · `decided_at` ts null ·
`decision` enum null · `suggestion_kind` text null.

`suggestion_kind` distinguishes a genuine ambiguity from a proactive cross-family creator suggestion
(FR-015), because the UI treats them differently and the two must not be counted together in queue
depth (SC-002's queue-volume risk signal).

Index `(decided_at)` — the open-queue query is `WHERE decided_at IS NULL`.

### `merge_log`

**Added table.** `id` bigint pk · `subject` enum · `operation` text (`merge` | `split`) ·
`winner_id` uuid · `loser_ids` json · `moved_credit_ids` json null · `performed_at` ts ·
`undone_at` ts null · `snapshot` json.

Required by FR-017: every merge, split, and queue decision must be reversible. `snapshot` holds the
pre-operation row state needed to restore, which is what makes undo possible without event sourcing
the whole database.

---

## 6. Providers, sync, and failures

### `providers`

`id` text pk · `enabled` bool · `status` enum · `acquisition` enum · `schema_version` int ·
`reviewed` bool (false for drop-in development providers — FR-041) · `config` json (DB overrides
only; file values are not copied here — R15) · `last_error` json null · `created_at`, `updated_at` ts.

### `provider_state`

**Added table** — the schedule (R1). `provider_id` text pk → providers.id ·
`cursor` json null (opaque, provider-owned) · `next_run_at` ts null · `effective_interval_seconds` int ·
`consecutive_failures` int · `retry_step` int · `last_success_at` ts null ·
`last_window_item_count` int null (the sanity baseline — R21) · `kv` json (the provider's own opaque
`state` store, FR-037).

`next_run_at` is the only thing the scheduler selects on: `WHERE enabled AND status <> 'disabled' AND
next_run_at <= now()`. Index `(next_run_at)`.

### `sync_runs`

`id` bigint pk · `provider_id` text · `lineage_id` uuid (groups retries of the same work — FR-019) ·
`attempt` int · `mode` text (`incremental` | `full` | `import`) · `status` enum ·
`started_at`, `finished_at` ts · `items_seen`, `items_written`, `items_failed` int ·
`error_class` enum null · `error_message` text null · `log_excerpt` text null (application logs, per
the spec's naming note) · `cursor_before`, `cursor_after` json null.

Indexes `(provider_id, started_at desc)`, `(lineage_id)`.

### `ingest_failures`

`id` bigint pk · `provider_id` text · `sync_run_id` bigint → sync_runs.id ·
`raw_payload` json (so a fixed plugin can replay it — FR-023) · `error` text · `stage` text
(`fetch` | `validate` | `normalize` | `write`) · `created_at` ts · `resolved_at` ts null.

### `sessions`

**Added table** (R12). `id` text pk (signed opaque) · `created_at` ts · `expires_at` ts ·
`token_fingerprint` text — so rotating `api.token` invalidates every existing session.

### `image_cache`

**Added table** (R10). `url_hash` text pk (sha256 of source URL) · `source_url` text ·
`bytes_sha256` text null · `content_type` text null · `size_bytes` int null · `fetched_at` ts null ·
`failed_at` ts null · `failure_count` int. Index `(bytes_sha256)` for byte-level deduplication.

### Search index

`search_index`: SQLite → FTS5 virtual table over `(kind, ref_id, text)`; Postgres → a table with a
`tsvector` column and a GIN index. Written by the ingest writer in the same transaction as the row it
describes (R5). Contents: work titles (all forms) and opinion review text.

### Migrations

Alembic, forward-only, applied automatically on startup with a pre-migration file copy of the SQLite
database (FR-049). Alembic owns its own version table; no hand-rolled `schema_migrations`.

**There is no `user_id` column anywhere** (FR-006). Adding one is a v2 schema break, accepted
knowingly.

---

## 7. State transitions

### Sync run

```
                 ┌─────────► success   (cursor advances; retry_step := 0)
running ─────────┼─────────► partial   (cursor advances to last checkpoint; retry ladder continues)
                 └─────────► failed    (cursor unchanged)
```

### Provider status

```
disabled ──enable──► idle ──due──► syncing ──ok──► idle
                       ▲              │
                       │              ├─recoverable failure──► idle (next_run_at = ladder step)
                       │              │      consecutive_failures ≥ threshold ──► degraded
                       │              └─auth | blocked | structure_changed────► degraded (no retry)
                       │
              misconfigured ◄── invalid config at startup (never blocks service start — FR-025)
```

`degraded` retries at the normal interval, never faster, and surfaces in the UI, `/health`, and the
application logs (FR-021, FR-051). `auth`, `blocked`, and `structure_changed` skip the ladder
entirely.

### Retry ladder

`retry_step` 0→1→2→3 maps to 1m, 5m, 15m, 1h; beyond that, the normal interval. Reset to 0 on any
`success`. A `rate_limit` classification additionally multiplies `effective_interval_seconds` for the
remainder of the session and honours `Retry-After` (FR-022).

### Resolution item

```
open ──operator──► linked | created | split | ignored     (decided_at set; reversible via merge_log)
```

### Entry lifecycle

```
absent ──ingest──► live ──provider reports delete──► tombstoned (deleted_at set)
                     └──inferred delete──► tombstoned   ONLY IF: provider lacks reports_deletes
                                                        AND infer_deletes = true (default false)
                                                        AND run was a completed `full` fetch
                                                        AND the run passed the sanity threshold
```

Three independent guards on the inferred path (R20), because this is the transition that destroys an
archive (FR-024, SC-014). Tombstoned rows are excluded from every read unless
`include_deleted=true`, and are never hard-deleted.
