# Implementation Plan: Aggregato — Self-Hosted Media Log Aggregator

**Branch**: `main` (no feature branch created) | **Date**: 2026-07-29 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-media-log-aggregator/spec.md`

## Summary

Build a single-user, self-hosted service that syncs a person's media logs from multiple third-party
platforms into one local database and serves them through one authenticated HTTP API plus a web UI.
Per-platform support is an in-repo plugin implementing a three-method contract; the host owns
scheduling, retrying, rate limiting, request pacing, ingest validation, identity resolution, and
migrations.

Technical approach: an async FastAPI application for the API, a **separate scheduler process** that
polls a database-backed due-queue and executes each sync run in a **short-lived child process** that
streams normalized JSON batches back over a pipe — which delivers the spec's crash/hang containment
(FR-025) and its "plugins never receive a database handle" rule (FR-037) from the same mechanism.
Storage is SQLAlchemy Core over SQLite (WAL) or Postgres with Alembic migrations. The frontend is a
Vue 3 SPA consuming only the public API (FR-031), built in a Docker stage so operators never install
Node.

## Technical Context

**Language/Version**: Python 3.13 (host, API, providers); TypeScript 5.x (frontend)

**Primary Dependencies**: FastAPI + uvicorn; Pydantic v2 (provider config models, normalized
payloads, settings); SQLAlchemy 2.0 Core (async) + Alembic; aiosqlite / asyncpg; httpx; PyYAML;
Vue 3 + Vite + vue-router. Standard library for scheduling, process isolation, and structured
logging — see [research.md](./research.md) R1, R2, R19.

**Storage**: SQLite (WAL) by default at `$AGGREGATO_DATA/aggregato.db`; Postgres optional via the
same URL setting. No Postgres-only column types (FR-048, spec Assumptions).

**Testing**: pytest + pytest-asyncio; httpx `ASGITransport` for API contract tests; a shared
**provider conformance suite** every bundled provider must pass (Constitution II); recorded
fixtures only — no test touches the network or needs credentials (FR-036, SC-010). vitest for the
one non-trivial frontend unit (the schema-driven settings form).

**Target Platform**: Linux, one Docker image, one volume, `docker compose up` (FR-048). Runs on
single-board-class hardware (SC-008).

**Project Type**: Web service (async API + scheduler worker) with an SPA frontend and an in-repo
plugin tree.

**Performance Goals** (Constitution VI — these are the declared budgets):

| Metric | Budget | Source |
|---|---|---|
| First page of filtered `/entries` at 1M entries | p95 < 1000 ms | SC-007 |
| Deep page (far end of 1M entries) | within 20% of first-page latency | SC-007 |
| Sustained ingest throughput | ≥ 50,000 entries/hour on 4-core ARM / 2 GB RAM | SC-008 |
| Ingest throughput at 1M rows vs at 10k rows | no measurable degradation (±10%) | SC-008 |
| Creator resolution | ≥ 20,000 credit lookups/minute, batched, O(1) amortized per credit | spec §5.8.2, SC-008 |
| Clean start to first browsable sync | < 15 min wall clock, operator time < 5 min | SC-001 |
| Fresh install outbound requests | exactly 0 | SC-013 |

**Constraints**: no outbound request to any non-platform host except images from platform-supplied
URLs (FR-010); no unauthenticated mode (FR-032); one bad provider must never block startup or
another provider (FR-025); provider-supplied data validated before any write (FR-008).

**Scale/Scope**: 1 user, ~10 bundled providers at maturity, 1M+ entries, 100k+ works, 200k+
creators, 54 functional requirements, 7 user stories, 7 milestones (M1–M7 in the source design).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` v1.0.0.

- [x] **I. Code Quality** — PASS. Ruff (format + lint) and mypy strict on `aggregato/`, zero
      warnings enforced in CI. No single-implementation interfaces: the only Protocol is `Provider`,
      which has many implementations by design. Search is the one place with two implementations
      (SQLite FTS5 / Postgres tsvector) and both are real (R5).
- [x] **II. Testing Standards** — PASS. Unit tests for every branch of resolution, normalization,
      retry classification, `subject_ref` validation, and cursor codec. The **provider conformance
      suite** is the contract test required by the constitution and doubles as the third-party
      compliance proof (FR-046). One integration test per user story, using real bundled providers
      against recorded fixtures. Determinism: `Clock` and `Random` are injected into the scheduler;
      `normalize` receives neither (R2, FR-036).
- [x] **III. UX Consistency** — PASS. Provider vocabulary is normalized in `normalize` and validated
      at the ingest boundary, so nothing platform-specific reaches the API (FR-008). One error shape
      (RFC 9457) for every failure. One date/precision rendering rule (FR-004). Empty / loading /
      degraded states are shared components. Keyboard-first resolution screens and a11y basics are
      requirements of US4, not polish.
- [x] **IV. Decoupling** — PASS. Dependency direction: `api` → `ingest`/`db`, `sync` →
      `ingest`/`providers`, `providers` → `domain` only. A provider cannot import `db` — enforced by
      an import-linter contract in CI, not by convention, because the constitution makes this a
      merge gate. The child-process boundary makes a DB handle physically unreachable from provider
      code.
- [x] **V. Plugin System** — PASS. Every source is a plugin using one contract; the bundled
      "fixture" provider in M1 is registered exactly like the real ones. Failure isolation: child
      process + wall-clock kill + output validation. No privileged first-party path.
- [x] **VI. Performance Budget** — PASS. Budgets declared in the table above; benchmark harness
      (`tests/bench/`) lands in M2 before AniList, per the source design's own instruction.

**Post-Phase-1 re-check**: PASS with one recorded deviation — the frontend is a built SPA rather
than server-rendered, which contradicts a spec Assumption. See Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/001-media-log-aggregator/
├── spec.md              # Feature specification
├── plan.md              # This file
├── research.md          # Phase 0 output — stack decisions with rationale
├── data-model.md        # Phase 1 output — tables, constraints, state transitions
├── quickstart.md        # Phase 1 output — how to run and validate each user story
├── contracts/
│   ├── openapi.yaml     # Phase 1 output — the public HTTP contract
│   └── provider-plugin.md  # Phase 1 output — the plugin contract + conformance obligations
├── checklists/
│   └── requirements.md  # Spec quality checklist (complete)
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
aggregato/
├── main.py                     # FastAPI app factory; API process entrypoint
├── worker.py                   # scheduler process entrypoint (separate process from the API)
├── config.py                   # YAML + ${ENV} + DB overrides, precedence, file-pinned markers
├── logging.py                  # stdlib logging + JSON formatter, run-scoped context
├── domain/
│   ├── enums.py                # MediaType, MediaFamily, Role, EntryKind, Capability, Acquisition,
│   │                           #   Confidence, ErrorClass, RunStatus — the closed vocabularies
│   ├── models.py               # RawRecord, NormalizedBatch, Cursor, CheckResult (Pydantic)
│   ├── subject_ref.py          # fixed-key validation (season/episode/track/disc/chapter/volume)
│   ├── families.py             # MediaType -> MediaFamily mapping
│   └── ratings.py              # linear + ordinal normalization
├── db/
│   ├── schema.py               # SQLAlchemy Core table definitions (dialect-portable)
│   ├── engine.py               # async engine, WAL + busy_timeout for SQLite
│   ├── search.py               # FTS5 / tsvector dialect implementations behind one function
│   └── migrations/             # Alembic, forward-only, auto-applied on startup
├── ingest/
│   ├── writer.py               # validated upserts, idempotency, tombstones
│   ├── resolve_work.py         # asserted -> matched -> create; queue on ambiguity
│   ├── resolve_creator.py      # family-scoped name match, batched, memo-cached
│   ├── normalize_replay.py     # rebuild derived rows from stored payloads
│   └── failures.py             # poison-record capture and replay
├── sync/
│   ├── scheduler.py            # due-queue poll loop, jitter, concurrency cap
│   ├── runner.py               # spawns and supervises one child per run; wall-clock kill
│   ├── child.py                # child entrypoint: fetch + normalize -> JSON lines on a pipe
│   ├── retry.py                # backoff ladder, lineage, degraded state
│   └── errors.py               # ErrorClass mapping and the never-retry set
├── providers/
│   ├── base.py                 # Provider Protocol, ProviderContext, RatingScale
│   ├── registry.py             # discovery: bundled tree, then drop-in dir (labelled unreviewed)
│   ├── http.py                 # rate limiting, politeness floors, UA, Retry-After, ETag
│   ├── fixture/                # M1 trivial provider — no network
│   ├── listenbrainz/           # M2
│   ├── anilist/                # M3
│   ├── goodreads/              # M4 (import only)
│   └── letterboxd/             # M5 (feed + import)
├── api/
│   ├── deps.py                 # bearer auth, session cookie, CSRF
│   ├── errors.py               # RFC 9457 problem+json
│   ├── pagination.py           # keyset cursor codec
│   └── routes/                 # works, entries, opinions, creators, providers, stats,
│                               #   resolution, failures, images, health, export, auth
├── images/cache.py             # content-addressed lazy fetch and store
└── export.py                   # portable backup artefact

frontend/                       # Vue 3 + Vite SPA; built in a Docker stage, never by the operator
├── src/api/                    # generated-from-openapi client, one fetch wrapper
├── src/views/                  # Dashboard, Log, Work, Creators, Creator, Providers,
│                               #   SyncHistory, Resolution, Stats, Settings, Login
└── src/components/SchemaForm.vue  # renders a provider's JSON Schema as a settings form

tests/
├── conformance/                # the shared provider conformance suite (contract tests)
├── contract/                   # API responses vs contracts/openapi.yaml
├── integration/                # one per user story, real providers + recorded fixtures
├── unit/
├── bench/                      # performance budgets from the table above (lands M2)
└── fixtures/<provider>/        # recorded payloads; the only data any test touches

docker/                         # multi-stage image, compose file
CONTRIBUTING.md                 # acquisition + scraping policy (M1 deliverable, per the design)
```

**Structure Decision**: Single Python package plus a separate frontend package. Not the template's
"web application" split, because there is no separate backend service boundary — one deployable, two
processes (API and scheduler) from the same package, which is what FR-025's containment requires and
FR-048's single-command deployment allows. The `providers/` tree is a package directory rather than
separate distributions because integrations ship with the core (FR-041).

## Complexity Tracking

> Filled because Phase 1 design deviates from a spec Assumption.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Vue 3 SPA frontend, contradicting the spec Assumption "server-rendered, no SPA build step in v1" | Requested directly by the operator for this plan. It also strengthens FR-031: an SPA physically cannot read the database or use a private endpoint, so "the UI consumes only the public API" stops being a discipline and becomes a property. | Server-rendered templates were the spec's choice specifically so a self-hoster needs no Node toolchain. That concern is answered by building the SPA in a Docker stage — operators run `docker compose up` and never install Node — but **building from source now requires Node**, which is a real cost, not an eliminated one. Spec Assumption should be amended to say so. |
| Two processes (API + scheduler) rather than one | FR-025 requires that a hanging or crashing provider never affect browsing or startup, and §6.7 requires provider work to be killable. A single-process design cannot guarantee either. | An asyncio task inside the API process covers a hang (via timeout) but not a hard crash or a C-extension deadlock, and it puts ingest CPU in the request path, which threatens SC-007. |
| A child process per sync run, on top of the scheduler process | Same containment requirement, plus it makes FR-037 ("no database handle, no other provider's secrets") physically true rather than reviewed. Cost is ~100 ms spawn per run, against run intervals measured in hours. | A persistent worker pool would keep provider state alive across runs and share one address space between providers — reintroducing exactly the leakage FR-037 forbids, for a saving that is invisible at this cadence. |
| Two search implementations (SQLite FTS5, Postgres tsvector) | FR-028 requires free-text search over titles and review text; SC-007 caps the first page at 1 s p95 over 1M entries. `LIKE '%…%'` cannot meet that. | A single portable `LIKE` scan fails the declared budget at scale. A third-party search engine violates FR-048 (no additional services). |
