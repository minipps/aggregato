---

description: "Task list for Aggregato — Self-Hosted Media Log Aggregator"
---

# Tasks: Aggregato — Self-Hosted Media Log Aggregator

**Input**: Design documents from `/specs/001-media-log-aggregator/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Tests**: MANDATORY per Constitution Principle II, and independently required by the spec itself
(FR-036, FR-046, SC-010). Every behavioural change lands with a test that fails first; every provider
passes the conformance suite; every user story has one integration test.

**Organization**: Tasks are grouped by user story so each story can be implemented, tested, and demoed
independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: US1–US7 from spec.md
- File paths are exact

## Path Conventions

Single Python package plus a frontend package, per plan.md Structure Decision:
`aggregato/` · `frontend/` · `tests/` · `docker/` at repository root.

---

> **Implementation status (2026-07-30)**: **Phase 1 and Phase 2 complete** (T001–T045). All gates
> verified clean on every commit: `ruff format`, `ruff check`, `mypy --strict` (45 files),
> `lint-imports` (3 contracts kept), `pytest` (390 passed, 1 skipped), plus the frontend's
> `type-check` and `build`. Both processes were smoke-tested from a cold start.
>
> Next: **Phase 4 (US2)** — cross-provider work and creator identity. Phase 3's provider, log API,
> and UI are complete. The OpenAPI pending list names the task owing every later endpoint.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization, quality gates, and the test infrastructure the constitution
requires before any behaviour exists.

- [X] T001 Create the package skeleton and `pyproject.toml` (Python 3.13, uv, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, httpx, PyYAML) with the directory tree from plan.md
- [X] T002 [P] Configure ruff format + lint and mypy strict for `aggregato/` in `pyproject.toml`, warnings-as-errors
- [X] T003 [P] Configure import-linter contracts in `pyproject.toml`: `aggregato.providers` may not import `aggregato.db`, `aggregato.ingest`, or `httpx`; `aggregato.api` may not import `aggregato.sync`
- [X] T004 [P] Configure pytest + pytest-asyncio and add the blocked-socket and frozen-clock fixtures in `tests/conftest.py` — network access in a test must fail
- [X] T005 [P] CI workflow running all four quality gates in `.github/workflows/ci.yml` (ruff, mypy, pytest, lint-imports)
- [X] T006 [P] Scaffold `frontend/` with Vite + Vue 3 + TypeScript + vue-router and a `npm run type-check` script
- [X] T007 [P] Multi-stage image in `docker/Dockerfile` (Node stage builds frontend, final stage has no toolchain) and `docker/compose.yml` with one volume and both processes
- [X] T008 [P] Write `CONTRIBUTING.md` with the acquisition hierarchy and scraping policy from contracts/provider-plugin.md §6 — the design requires this before the repo is public, not before the first scraper
- [X] T009 [P] Add `.env.example` and README run instructions matching quickstart.md
- [X] T010 [P] Document fixture recording conventions in `tests/fixtures/README.md` (one directory per provider, JSON lines, no credentials, no live capture in CI)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The M1 core skeleton. Every user story depends on all of it.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Domain vocabulary

- [X] T011 [P] Implement all closed vocabularies in `aggregato/domain/enums.py` per data-model.md §1, with a helper that emits CHECK constraints from a StrEnum
- [X] T012 [P] Implement `media_type` → `media_family` mapping in `aggregato/domain/families.py` with a unit test asserting every media type maps exactly once, in `tests/unit/test_families.py`
- [X] T013 [P] Implement `subject_ref` validation in `aggregato/domain/subject_ref.py` (six keys, positive ints, `extra="forbid"`, empty object rejected) with `tests/unit/test_subject_ref.py` covering each rejection branch
- [X] T014 [P] Implement linear and ordinal rating normalization in `aggregato/domain/ratings.py` with `tests/unit/test_ratings.py` covering both kinds, boundary values, and an ordinal scale missing a mapping
- [X] T015 [P] Define `RawRecord`, `Checkpoint`, `Cursor`, `NormalizedBatch` and its members, and `CheckResult` in `aggregato/domain/models.py` per contracts/provider-plugin.md §3, with `logged_precision` required and no default

### Storage

- [X] T016 Define `works`, `external_ids`, `provider_items` in `aggregato/db/schema.py` using the portability rules in research.md R4
- [X] T017 Define `entries`, `opinions`, `rating_scales` in `aggregato/db/schema.py`, including the partial unique index on `(provider_id, native_id) WHERE native_id IS NOT NULL` and the keyset sort indexes
- [X] T018 Define `creators`, `creator_aliases`, `creator_external_ids`, `work_credits` (with `link_confidence`) in `aggregato/db/schema.py`
- [X] T019 Define `providers`, `provider_state`, `sync_runs`, `ingest_failures`, `resolution_queue`, `merge_log`, `sessions`, `image_cache` in `aggregato/db/schema.py`
- [X] T020 Implement the async engine in `aggregato/db/engine.py` with SQLite WAL + `busy_timeout` and Postgres support from the same URL setting
- [X] T021 Create the initial Alembic revision in `aggregato/db/migrations/`, applied automatically on startup with a pre-migration file copy of the SQLite database
- [X] T022 Implement dialect search in `aggregato/db/search.py` (SQLite FTS5, Postgres tsvector) behind one interface, with `tests/unit/test_search.py` running against both dialects
- [X] T023 [P] Implement the dialect upsert helper in `aggregato/db/upsert.py` with a unit test in `tests/unit/test_upsert.py`

### Configuration and logging

- [X] T024 Implement configuration in `aggregato/config.py` — defaults ← YAML ← `${ENV}` ← database overrides, file-pinned reporting, secrets never written back — with `tests/unit/test_config.py` asserting precedence, that a missing `api.token` is fatal, and that an invalid provider block disables only that provider
- [X] T025 [P] Implement JSON-formatted structured logging with a contextvars run binding in `aggregato/logging.py`

### Provider host

- [X] T026 Define the `Provider` Protocol, `ProviderContext`, and `RatingScale` in `aggregato/providers/base.py` exactly as contracts/provider-plugin.md §1 specifies
- [X] T027 [P] Define `AuthError`, `BlockedError`, `StructureChangedError`, `RateLimited` in `aggregato/providers/errors.py`
- [X] T028 Implement bundled-tree discovery in `aggregato/providers/registry.py`, inert until explicitly enabled
- [X] T029 Implement the host HTTP client in `aggregato/providers/http.py` — token bucket at `max(declared, host_floor)`, per-host `Semaphore(1)` for `scrapes`, retry with jitter, `Retry-After`, ETag pass-through, identifying User-Agent — with `tests/unit/test_http_politeness.py` asserting a provider cannot raise the floor or exceed one in-flight request per host
- [X] T030 [P] Implement the trivial no-network `fixture` provider in `aggregato/providers/fixture/` for M1

### Sync machinery

- [X] T031 [P] Implement error classification and the never-retry set in `aggregato/sync/errors.py` with `tests/unit/test_error_classification.py` asserting `auth`, `blocked`, and `structure_changed` never schedule a retry
- [X] T032 Implement the retry ladder, lineage, and degraded threshold in `aggregato/sync/retry.py` with `tests/unit/test_retry_ladder.py` using the injected clock
- [X] T033 Implement the child entrypoint and JSON-lines protocol in `aggregato/sync/child.py`
- [X] T034 Implement spawn, supervision, wall-clock kill, and parent-side line validation in `aggregato/sync/runner.py`
- [X] T035 Implement the due-queue poll loop with jitter and a concurrency cap in `aggregato/sync/scheduler.py` with `tests/unit/test_scheduler.py` using the injected clock
- [X] T036 Implement the scheduler process entrypoint in `aggregato/worker.py`

### Ingest

- [X] T037 Implement the validated writer in `aggregato/ingest/writer.py` — upsert idempotency, the `(provider_item_id, kind, logged_at, subject_ref)` fallback for entries without a native id, search index maintenance in the same transaction — with `tests/unit/test_writer_idempotency.py`
- [X] T038 [P] Implement poison-record capture in `aggregato/ingest/failures.py` so one bad record never blocks a run

### API foundation

- [X] T039 Implement the app factory, static asset serving, and the startup migration hook in `aggregato/main.py`
- [X] T040 [P] Implement RFC 9457 problem+json handlers in `aggregato/api/errors.py` with `tests/contract/test_problem_shape.py`
- [X] T041 Implement auth in `aggregato/api/deps.py` — bearer, session exchange, CSRF for cookie writes, constant-time token compare, rotation invalidating sessions — with `tests/contract/test_auth.py` asserting no endpoint is reachable unauthenticated
- [X] T042 [P] Implement the keyset cursor codec in `aggregato/api/pagination.py` with `tests/unit/test_pagination.py` covering round-trip and tied sort keys
- [X] T043 [P] Implement `GET /health` in `aggregato/api/routes/health.py`
- [X] T044 Build the conformance suite skeleton in `tests/conformance/` with all nine assertion groups from contracts/provider-plugin.md §5, registered for the `fixture` provider
- [X] T045 [P] Build the OpenAPI validation harness in `tests/contract/test_openapi.py` comparing served responses to `contracts/openapi.yaml`

**Checkpoint**: Foundation ready — user story implementation can begin in parallel.

---

## Phase 3: User Story 1 - One platform's history, local and browsable (Priority: P1) 🎯 MVP

**Goal**: Deploy, enable one platform, sync, browse the result in the web UI.

**Independent Test**: From a clean state, enable exactly one platform, wait for a sync, and confirm
browsed entries match what the platform shows.

### Tests for User Story 1 (MANDATORY - Constitution Principle II) ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [X] T046 [P] [US1] Register `listenbrainz` in the conformance suite in `tests/conformance/test_providers.py`
- [X] T047 [P] [US1] Integration test for the single-provider journey in `tests/integration/test_us1_single_provider.py` — enable, sync, browse, resync writes nothing
- [X] T048 [P] [US1] Integration test asserting a fresh install makes zero outbound requests in `tests/integration/test_fresh_install_is_silent.py` (SC-013)
- [X] T049 [P] [US1] Integration test asserting interrupted syncs lose and duplicate nothing in `tests/integration/test_idempotent_interrupted_sync.py` (SC-003)
- [X] T050 [P] [US1] Contract tests for `/entries`, `/works`, `/opinions`, `/providers` in `tests/contract/test_log_endpoints.py`

### Implementation for User Story 1

- [X] T051 [P] [US1] Record ListenBrainz fixtures in `tests/fixtures/listenbrainz/`
- [X] T052 [US1] Implement the ListenBrainz provider in `aggregato/providers/listenbrainz/` — paged fetch with mid-fetch checkpoints, MBID extraction, pure normalize (depends on T051)
- [X] T053 [US1] Implement `GET /entries` with all filters, sorts, and keyset paging in `aggregato/api/routes/entries.py`
- [X] T054 [US1] Implement `GET /works` and `GET /works/{id}` in `aggregato/api/routes/works.py`
- [X] T055 [P] [US1] Implement `GET /opinions` in `aggregato/api/routes/opinions.py`
- [X] T056 [US1] Implement `GET /providers`, enable/disable, `POST /providers/{id}/sync`, `POST /providers/{id}/check` in `aggregato/api/routes/providers.py`
- [X] T057 [P] [US1] Implement the typed API client and fetch wrapper in `frontend/src/api/`
- [X] T058 [P] [US1] Implement the Login view and session exchange in `frontend/src/views/Login.vue`
- [X] T059 [US1] Implement the shared date-and-precision renderer in `frontend/src/components/LoggedAt.vue` with `frontend/src/components/__tests__/LoggedAt.spec.ts` asserting a month-only date never renders as a time
- [X] T060 [US1] Implement the Log view with filters and cursor paging in `frontend/src/views/Log.vue`
- [X] T061 [US1] Implement the Work detail view in `frontend/src/views/Work.vue`
- [X] T062 [US1] Implement the Providers view — enable, configure, sync now, check credentials — in `frontend/src/views/Providers.vue`
- [X] T063 [P] [US1] Implement the Dashboard with recent entries and per-provider health in `frontend/src/views/Dashboard.vue`

**Checkpoint**: A real archive from one platform, browsable. Demoable MVP.

---

## Phase 4: User Story 2 - Two platforms, one log (Priority: P1)

**Goal**: The same item logged on two platforms appears once, with both opinions side by side.

**Independent Test**: Enable two platforms with overlapping accounts; shared items unify, distinct
items stay distinct, ambiguous ones queue.

### Tests for User Story 2 (MANDATORY - Constitution Principle II) ⚠️

- [X] T064 [P] [US2] Build the benchmark harness and 1M-entry seed script in `tests/bench/` — Constitution VI requires budgets measured, and the design requires this before AniList lands
- [X] T065 [P] [US2] Integration test for cross-provider identity in `tests/integration/test_us2_cross_provider_identity.py` — shared identifier unifies, title+year unifies only when unambiguous, ambiguous queues, same-titled different works stay separate
- [X] T066 [P] [US2] Unit tests for every work-resolution branch in `tests/unit/test_resolve_work.py`
- [X] T067 [P] [US2] Unit tests for creator resolution in `tests/unit/test_resolve_creator.py` — asserted identifiers unscoped, name matching family-scoped, fuzzy never auto-linked
- [X] T068 [P] [US2] Benchmark test for batched creator resolution in `tests/bench/test_creator_resolution.py` (≥20,000 lookups/min, flat as creators grow)
- [X] T069 [P] [US2] Benchmark tests for first-page and deep-page latency in `tests/bench/test_query_latency.py` (SC-007)

### Implementation for User Story 2

- [X] T070 [US2] Implement title normalization in `aggregato/ingest/titles.py` — case folding, accent stripping, article handling, punctuation, common edition suffixes — with `tests/unit/test_titles.py`
- [X] T071 [US2] Implement work resolution in `aggregato/ingest/resolve_work.py` — asserted → matched → create, duplicate preferred over uncertain merge
- [X] T072 [US2] Implement batched, memo-cached creator resolution in `aggregato/ingest/resolve_creator.py` per research.md R8
- [X] T073 [US2] Implement resolution queue creation in `aggregato/ingest/resolve_queue.py`, including cross-family creator suggestions tagged with `suggestion_kind`
- [X] T074 [US2] Implement `GET /creators` and `GET /creators/{id}` with credits grouped by role and logged counts in `aggregato/api/routes/creators.py`
- [X] T075 [P] [US2] Record AniList fixtures in `tests/fixtures/anilist/`, including a season-level entry and staff/studio credits
- [X] T076 [US2] Implement the AniList provider in `aggregato/providers/anilist/` — OAuth, GraphQL, ordinal rating scales, staff and studio credits with identifiers, season-level works (depends on T075)
- [X] T077 [US2] Implement the content-addressed lazy image cache in `aggregato/images/cache.py`
- [X] T078 [US2] Implement `GET /media/image/{hash}` with the placeholder path when caching is off in `aggregato/api/routes/images.py`
- [X] T079 [P] [US2] Implement the Creators list and detail views in `frontend/src/views/Creators.vue` and `frontend/src/views/Creator.vue`
- [X] T080 [US2] Add side-by-side cross-provider opinions to `frontend/src/views/Work.vue`, with the within-scale-only caveat visible rather than documented
- [X] T081 [US2] Add the composite indexes each permitted `sort` value needs and verify plans in `tests/bench/test_query_plans.py`
- [X] T082 [US2] Record the performance baseline in `tests/bench/baseline.json`

**Checkpoint**: Two platforms, one unified log, with the declared budgets measured.

---

## Phase 5: User Story 3 - Sync the operator can trust (Priority: P2)

**Goal**: Recover automatically where possible, say exactly what is wrong where not, never destroy
history, never let one platform affect another.

**Independent Test**: Force every failure class and confirm the recorded outcome, retry behaviour, and
operator-facing message for each.

### Tests for User Story 3 (MANDATORY - Constitution Principle II) ⚠️

- [X] T083 [P] [US3] Integration test covering the full failure matrix in `tests/integration/test_us3_failure_matrix.py` — all ten cases from quickstart.md, one parametrized case per row
- [X] T084 [P] [US3] Integration test asserting no inferred deletion for a provider without `reports_deletes` in `tests/integration/test_no_inferred_deletes.py` (SC-014)
- [X] T085 [P] [US3] Unit test for the sanity threshold in `tests/unit/test_sanity_threshold.py`
- [X] T086 [P] [US3] Integration test asserting a killed or hung provider child leaves the API serving and other providers syncing in `tests/integration/test_provider_containment.py` (FR-025)

### Implementation for User Story 3

- [X] T087 [US3] Implement the sanity threshold and `last_window_item_count` tracking in `aggregato/sync/sanity.py`
- [X] T088 [US3] Implement the triple-guarded delete-inference path in `aggregato/ingest/writer.py` — `reports_deletes` absent, `infer_deletes` true, completed `full` run, sanity threshold passed
- [X] T089 [US3] Implement partial-run status and checkpoint-bounded cursor advance in `aggregato/sync/runner.py`
- [X] T090 [US3] Implement `action_required` messages per error class in `aggregato/sync/errors.py` so no failure surfaces generically (SC-005)
- [X] T091 [US3] Implement rate-limit session-interval stretching in `aggregato/sync/retry.py`
- [X] T092 [US3] Implement ingest-failure replay from stored payloads in `aggregato/ingest/failures.py`
- [X] T093 [US3] Implement `GET /providers/{id}/runs` and `GET /ingest-failures` in `aggregato/api/routes/runs.py`
- [X] T094 [US3] Extend `GET /health` with per-provider status, last success, and consecutive failures in `aggregato/api/routes/health.py`
- [X] T095 [P] [US3] Implement the Sync history view with retry-lineage grouping in `frontend/src/views/SyncHistory.vue`
- [X] T096 [P] [US3] Implement the Ingest failures view with payload detail and replay in `frontend/src/views/IngestFailures.vue`
- [X] T097 [P] [US3] Implement the degraded banner and per-provider status chips in `frontend/src/components/ProviderStatus.vue`

**Checkpoint**: The service can be left alone.

---

## Phase 6: User Story 4 - Fixing identity by hand (Priority: P2)

**Goal**: Clear the resolution queue, merge duplicates, split wrongly-joined creators, undo anything.

**Independent Test**: Seed known-ambiguous and known-conflated data and resolve it entirely through
the UI.

### Tests for User Story 4 (MANDATORY - Constitution Principle II) ⚠️

- [X] T098 [P] [US4] Integration test for merge, split, and undo in `tests/integration/test_us4_merge_split.py`
- [X] T099 [P] [US4] Unit tests for `merge_log` snapshot and restore in `tests/unit/test_merge_log.py`
- [X] T100 [P] [US4] Integration test asserting a manual decision survives a resync of both providers in `tests/integration/test_manual_durability.py` (FR-014)

### Implementation for User Story 4

- [X] T101 [US4] Implement work merge in `aggregato/ingest/merge.py` — entries, opinions, and identifiers combine with nothing lost
- [X] T102 [US4] Implement creator merge in `aggregato/ingest/merge.py`, retaining aliases from every family
- [X] T103 [US4] Implement credit-granular creator split in `aggregato/ingest/split.py`, exposing `link_confidence` per credit
- [X] T104 [US4] Implement `merge_log` writes and `POST /merge-log/{id}/undo` in `aggregato/api/routes/identity.py`
- [X] T105 [US4] Implement `POST /works/{id}/merge`, `POST /creators/{id}/merge`, `POST /creators/{id}/split` in `aggregato/api/routes/identity.py`
- [X] T106 [US4] Implement `GET /resolution-queue` and `POST /resolution-queue/{id}/decide` recording `manual` confidence in `aggregato/api/routes/resolution.py`
- [X] T107 [US4] Implement the keyboard-driven Resolution view with candidate reasons in `frontend/src/views/Resolution.vue`
- [X] T108 [P] [US4] Implement the merge and split UI with side-by-side comparison and `link_confidence` display in `frontend/src/components/MergeDialog.vue` and `frontend/src/components/SplitDialog.vue`
- [X] T109 [P] [US4] Implement undo affordance and keyboard shortcuts, with an accessibility test in `frontend/src/views/__tests__/Resolution.spec.ts`

**Checkpoint**: Automatic matching is correctable, which is what makes the no-enrichment trade viable.

---

## Phase 7: User Story 5 - Platforms with no API (Priority: P3)

**Goal**: Upload a personal export file and have it land in the same archive.

**Independent Test**: Upload a real export file and confirm entries, ratings, and identifiers appear
in the unified log.

### Tests for User Story 5 (MANDATORY - Constitution Principle II) ⚠️

- [X] T110 [P] [US5] Integration test for file import in `tests/integration/test_us5_file_import.py` — ingests, re-upload adds nothing, unrelated file rejected with the archive unchanged
- [X] T111 [P] [US5] Integration test for title-and-year-only matching against Letterboxd fixtures in `tests/integration/test_weak_matching.py` (the weakest case under FR-011)

### Implementation for User Story 5

- [X] T112 [US5] Implement `import` FetchMode plumbing and `ctx.import_path` in `aggregato/sync/runner.py` and `aggregato/sync/child.py`
- [X] T113 [US5] Implement `POST /providers/{id}/import` with multipart upload, type validation, and unchanged-on-rejection semantics in `aggregato/api/routes/providers.py`
- [X] T114 [P] [US5] Record Goodreads CSV fixtures in `tests/fixtures/goodreads/`
- [X] T115 [US5] Implement the Goodreads provider in `aggregato/providers/goodreads/` — CSV parsing, ISBN and ISBN-13 extraction, import-only capabilities (depends on T114)
- [X] T116 [P] [US5] Record Letterboxd fixtures in `tests/fixtures/letterboxd/`, including a structurally-changed page that must raise `StructureChangedError`
- [X] T117 [US5] Implement the Letterboxd provider in `aggregato/providers/letterboxd/` — RSS for recent activity plus CSV for history, combining two acquisition surfaces rather than scraping what the feed provides (depends on T116)
- [X] T118 [US5] Add the import upload flow to `frontend/src/views/Providers.vue`

**Checkpoint**: Platforms with no API are first-class, proving the contract is general.

---

## Phase 8: User Story 6 - Adding support for a new platform (Priority: P3)

**Goal**: A contributor adds a platform against the documented contract with zero core changes.

**Independent Test**: Write a new integration using only recorded fixtures and no credentials; it
syncs and appears in the UI with no core modification.

### Tests for User Story 6 (MANDATORY - Constitution Principle II) ⚠️

- [X] T119 [P] [US6] Complete all nine conformance assertion groups in `tests/conformance/` and register every bundled provider
- [X] T120 [P] [US6] Integration test asserting a `schema_version` bump replays the archive with no network in `tests/integration/test_replay.py` (SC-011)
- [X] T121 [P] [US6] Integration test walking the authoring guide end to end for a throwaway provider, asserting zero core file changes in `tests/integration/test_new_provider_tutorial.py` (SC-009)

### Implementation for User Story 6

- [X] T122 [US6] Implement `GET /providers/{id}/config-schema` with secrets marked `writeOnly` in `aggregato/api/routes/providers.py`
- [X] T123 [US6] Implement schema-driven settings form rendering in `frontend/src/components/SchemaForm.vue` with `frontend/src/components/__tests__/SchemaForm.spec.ts`
- [X] T124 [US6] Implement drop-in directory discovery and the `unreviewed` label in `aggregato/providers/registry.py`, with the enable-action warning in `frontend/src/views/Providers.vue`
- [X] T125 [US6] Implement scraping disclosure — `acquisition` label, risk statement at the point of enabling — in `frontend/src/views/Providers.vue`
- [X] T126 [US6] Implement the normalization replay job in `aggregato/ingest/normalize_replay.py`, triggered when a provider's `schema_version` exceeds the stored maximum
- [X] T127 [US6] Implement the provider-API version declaration and the drop-in compatibility warning in `aggregato/providers/registry.py`
- [X] T128 [P] [US6] Write the provider authoring guide in `docs/writing-a-provider.md`, referencing contracts/provider-plugin.md as the normative contract

**Checkpoint**: Platform coverage can grow through contribution without core changes.

---

## Phase 9: User Story 7 - Owning the archive (Priority: P3)

**Goal**: Portable backup, visible storage cost, switchable retention.

**Independent Test**: Produce a backup, restore into a fresh instance, confirm the archive is intact
and browsable.

### Tests for User Story 7 (MANDATORY - Constitution Principle II) ⚠️

- [X] T129 [P] [US7] Integration test for export and restore in `tests/integration/test_us7_export_restore.py` — no secrets in the archive, restore requires zero syncs

### Implementation for User Story 7

- [X] T130 [US7] Implement the portable archive builder in `aggregato/export.py` (database dump plus configuration, secrets excluded)
- [X] T131 [US7] Implement `GET /export` as a streaming response in `aggregato/api/routes/export.py`
- [X] T132 [US7] Implement storage usage reporting broken out by raw payload retention and image cache in `aggregato/api/routes/settings.py`
- [X] T133 [US7] Implement retention settings and the cleanup job in `aggregato/db/retention.py`, keeping failures longer than successes by default
- [X] T134 [US7] Implement the image cache toggle and its placeholder behaviour in `aggregato/images/cache.py`
- [X] T135 [P] [US7] Implement the Settings view — global config, token rotation, image cache, storage usage, export — in `frontend/src/views/Settings.vue`

**Checkpoint**: The archive is genuinely the operator's.

---

## Phase 10: Polish & Cross-Cutting Concerns

- [X] T136 [P] Implement `GET /stats/summary` and `GET /stats/top` in `aggregato/api/routes/stats.py`, with the shared aggregate builder excluding sub-unit records by default
- [X] T137 [P] Add `tests/unit/test_subunit_aggregate_default.py` asserting the default direction explicitly, with a failure message naming the corruption it prevents
- [X] T138 [P] Implement the Stats view in `frontend/src/views/Stats.vue`
- [X] T139 [P] Accessibility sweep across every view — keyboard reachability, semantic structure, text alternatives — recorded in `docs/accessibility.md`
- [X] T140 [P] Light and dark theme plus density pass in `frontend/src/styles/`, designed for typography rather than assuming cover art
- [X] T141 [P] Document `structure_changed` → `docker compose pull` as the standard operator response in `docs/operations.md`
- [X] T142 [P] Add the run-history retention defaults and storage growth note to `docs/operations.md`
- [X] T143 Run the full quickstart.md validation end to end on a clean machine and record results
- [X] T144 Final Constitution v1.0.0 gate check — four quality gates clean, conformance green, budgets within 10% of `baseline.json`, no undocumented violation

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies
- **Foundational (Phase 2)**: depends on Setup — **blocks every user story**
- **User Stories (Phases 3–9)**: all depend only on Foundational; they can then proceed in parallel or
  in priority order
- **Polish (Phase 10)**: depends on US1 and US2 for stats and theming to have data to show

### User Story Dependencies

- **US1 (P1)**: independent after Foundational — the MVP
- **US2 (P1)**: independent after Foundational. Shares `frontend/src/views/Work.vue` with US1 (T080
  edits what T061 creates) and adds indexes to tables T017 defines — sequence those two files if both
  stories are staffed at once
- **US3 (P2)**: independent after Foundational. Extends `aggregato/sync/retry.py`, `errors.py`, and
  `writer.py` from Phase 2 rather than replacing them
- **US4 (P2)**: needs the resolution queue to contain something, so it is *demoable* only after US2 —
  but its code and tests do not depend on US2 and can be built with seeded queue rows
- **US5 (P3)**: independent after Foundational
- **US6 (P3)**: benefits from at least two real providers existing (US1, US2) to register in the
  conformance suite; the contract work itself is independent
- **US7 (P3)**: independent after Foundational

### Within Each User Story

- Tests MUST be written and FAIL before implementation
- Fixtures before the provider that reads them (T051→T052, T075→T076, T114→T115, T116→T117)
- Domain and schema before ingest; ingest before endpoints; endpoints before the views that call them
- Story complete and demoable before moving to the next priority

### Parallel Opportunities

- Phase 1: T002–T010 all parallel after T001
- Phase 2: T011–T015 parallel; T016–T019 touch one file so they serialize; T023, T025, T027, T030,
  T031, T038, T040, T042, T043, T045 parallel
- Phase 3: T046–T050 parallel (tests); T055, T057, T058, T063 parallel; T051 parallel with any test
- Phase 4: T064–T069 parallel (tests and benchmarks); T075, T079 parallel
- Phase 5: T083–T086 parallel; T095–T097 parallel
- Phase 6: T098–T100 parallel; T108, T109 parallel
- Phase 9–10: T129 parallel; T136–T142 all parallel
- Across stories: once Phase 2 is done, US1, US3, US5, and US7 can be worked by four people with
  almost no file contention

## Parallel Example: User Story 1

```bash
# Tests first — all four fail before any implementation exists
Task: "Register listenbrainz in the conformance suite in tests/conformance/test_providers.py"
Task: "Integration test single-provider journey in tests/integration/test_us1_single_provider.py"
Task: "Integration test zero outbound requests in tests/integration/test_fresh_install_is_silent.py"
Task: "Contract tests for log endpoints in tests/contract/test_log_endpoints.py"

# Then the independent implementation files
Task: "Record ListenBrainz fixtures in tests/fixtures/listenbrainz/"
Task: "Implement GET /opinions in aggregato/api/routes/opinions.py"
Task: "Implement the API client in frontend/src/api/"
Task: "Implement the Login view in frontend/src/views/Login.vue"
```

---

## Implementation Strategy

### Mapping to the design's milestones

| Design milestone | Phases here |
|---|---|
| M1 core skeleton | Phase 1 + Phase 2 (+ T008 `CONTRIBUTING.md`, which M1 requires before the repo is public) |
| M2 ListenBrainz, first real provider, benchmark | Phase 3, plus T064/T068 pulled forward from Phase 4 |
| M3 resolution + AniList | Phase 4 and Phase 6 (US4) |
| M4 import path + Goodreads | Phase 7 |
| M5 Letterboxd, feeds and edges | Phase 7 |
| M6 UI | **Distributed across every story** rather than deferred |
| M7 operator polish | Phase 9 and Phase 10 |

The one deliberate departure: the design lands the whole UI at M6, but US1 is only demoable with a
browsable log, so each story ships its own screens. The API-first constraint still holds — every view
consumes only the public contract (FR-031), which the SPA enforces structurally.

### MVP First (User Story 1 only)

1. Phase 1 → Phase 2 → Phase 3
2. **Stop and validate**: one real platform, synced, browsable, idempotent on resync
3. This is a genuinely useful personal archive; ship it

### Incremental Delivery

1. Setup + Foundational → foundation ready
2. + US1 → one platform archived (MVP)
3. + US2 → the actual product: platforms unified, with budgets measured
4. + US3 → leave it running unattended
5. + US4 → correct what automatic matching got wrong
6. + US5, US6, US7 → coverage, contribution, and ownership
7. Polish

### Parallel Team Strategy

After Phase 2: one person on US1 then US2 (the identity core, and the highest-risk work), one on US3
(failure matrix — almost no file overlap), one on US5 and US7 (import and operations). US4 joins once
US2 produces queue rows. US6 last, since it consolidates what the earlier providers taught.

---

## Notes

- [P] means different files and no dependency on an incomplete task
- Every task names an exact path; the only shared-file sequencing risks are called out in Dependencies
- Verify tests fail before implementing — the constitution makes this a merge gate, not a preference
- Commit per task or per logical group; stop at any checkpoint to validate a story independently
- The four permanently-load-bearing tests are T048 (silent install), T084 (no inferred deletes), T049
  (idempotent interrupted sync), and T137 (sub-unit aggregate default). Each guards a failure mode the
  spec calls silently destructive; none should ever be marked skipped
