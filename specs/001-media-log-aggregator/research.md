# Phase 0 Research: Aggregato

**Date**: 2026-07-29 | **Plan**: [plan.md](./plan.md)

Operator-supplied stack: FastAPI (async), Vue for the frontend, `fastapi-crons` "probably" for
tasks with suggestions invited. Everything below either confirms that, or says plainly where it
does not fit and what replaces it. No `NEEDS CLARIFICATION` remains.

---

## R1 — Scheduling: reject cron-style scheduling, use a database due-queue

**Decision**: No scheduling library. `provider_state.next_run_at` in the database is the schedule.
One `asyncio` loop in the scheduler process wakes every few seconds, selects providers where
`next_run_at <= now()` and status is not `disabled`, and dispatches up to a concurrency cap. Every
run's outcome rewrites `next_run_at`.

**Rationale**: What §7 requires is not a calendar. It is: a per-provider interval that the *plugin*
declares (FR-018), jitter on boot, an escalating retry ladder that overrides the interval after a
failure (1m → 5m → 15m → 1h → normal, FR-020), a `degraded` state that collapses the ladder back to
the normal interval (FR-021), a rate-limit response that *lengthens* the effective interval for the
rest of the session (FR-022), and a full record of every attempt with lineage (FR-019). All of that
is state that must survive restart and be visible in the UI — so it has to be in the database
regardless of which scheduler triggers it. Once `next_run_at` is persisted, the scheduler is a
`SELECT … WHERE next_run_at <= now()` and a loop: about 40 lines, no dependency, and observability
comes free because the schedule *is* a queryable table.

**Alternatives considered**:
- **`fastapi-crons`** — cron-expression schedulers of this family bind jobs to fixed calendar
  expressions registered at import time. Aggregato's next run time is computed *after* each run from
  its outcome, and providers are enabled and disabled at runtime. Expressing "retry this one
  provider in 5 minutes, then 15" as a rewritten cron expression is fighting the model. Rejected on
  fit, not quality.
- **APScheduler** with an async scheduler and a persistent job store — closer, and it does support
  dynamic `run_date` rescheduling. Rejected because it duplicates state we must keep anyway
  (`sync_runs`, `next_run_at`) in a second store, and because its job-store abstraction adds a
  dependency to do less than the query above.
- **Celery / ARQ / Dramatiq** — all require a broker, violating FR-048 (no additional services).
- **System cron in the container** — no per-run state, no lineage, no degraded state, and it cannot
  see whether a run is still in flight.

**Consequence**: `provider_state` is part of the M1 schema, and the scheduler loop needs an injected
`Clock` so tests can advance time deterministically (Constitution II).

## R2 — Provider isolation: one child process per sync run

**Decision**: `multiprocessing` with the `spawn` start method. Per run, the scheduler spawns a child
running `sync/child.py`, which imports only that one provider, runs `fetch` and `normalize`, and
writes **JSON lines** to a pipe: `{"type": "batch", …}`, `{"type": "checkpoint", "cursor": …}`,
`{"type": "error", "class": …}`. The parent validates each line against the normalized Pydantic
models and performs every database write itself.

**Rationale**: Three separate spec requirements collapse into this one mechanism — hang and crash
containment (FR-025), no database handle or foreign secrets in provider code (FR-037), and a
killable per-run wall-clock timeout (§6.7). A child process gives all three by construction rather
than by review. JSON lines rather than pickle because the payload crosses a boundary from
plugin-controlled code, and because it is the same shape the recorded fixtures use — one wire format
for production and tests (FR-036, FR-046).

**Alternatives considered**:
- **asyncio task in the API process** — cannot survive a hard crash, cannot be killed reliably if
  the provider blocks the event loop, and puts ingest CPU in the request path against SC-007.
- **Persistent worker pool** — keeps providers co-resident in one address space, reintroducing the
  leakage FR-037 forbids, and per-run spawn cost (~100 ms) is irrelevant at hourly cadence.
- **Subprocess sandbox / WASM** — the spec's own §6.7 says this is the answer *if* out-of-tree
  providers ever become supported, and explicitly not before. Building it now would be speculative.

## R3 — Data access: SQLAlchemy 2.0 Core + Alembic, no ORM

**Decision**: SQLAlchemy Core (`Table` objects, explicit `select`/`insert`), async engine, Alembic
for forward-only migrations applied on startup. No declarative ORM models.

**Rationale**: The workload is bulk upsert and analytical filtering, not object graph navigation. An
identity map and lazy loading are liabilities at 50k entries/hour (SC-008), and the hot paths are
already written as set operations (R8). Core also keeps the dual-dialect work explicit and visible
instead of hidden behind ORM defaults. Alembic is the boring standard for FR-049.

**Alternatives considered**: SQLModel/SQLAlchemy ORM (per-row object overhead on the ingest path);
raw asyncpg/aiosqlite with hand-written SQL (two dialects × every query — more code than Core, not
less); Tortoise/Piccolo (weaker migration story, no benefit here).

## R4 — Dual-dialect portability rules

**Decision**: A fixed set of type choices, applied consistently:

| Concern | Choice |
|---|---|
| UUID keys | `sqlalchemy.Uuid` — native `uuid` on Postgres, `CHAR(32)` on SQLite |
| Auto keys | `BigInteger` with a `.with_variant(sqlite.INTEGER, "sqlite")` so it aliases rowid |
| Timestamps | `DateTime(timezone=True)`; **always stored UTC**, converted at the API edge only |
| JSON columns | `sqlalchemy.JSON` with a `JSONB` variant on Postgres |
| Enums | Python `StrEnum` + a `CHECK` constraint on a text column, not native DB enums |
| Upsert | dialect `on_conflict_do_update` (both dialects support it) behind one writer helper |
| Partial unique | `Index(..., sqlite_where=…, postgresql_where=…)` — supported by both |

**Rationale**: FR-048 and the spec's Assumption require both dialects to stay first-class. Native DB
enums are the trap — every added `media_type` becomes a Postgres `ALTER TYPE` migration with no
SQLite equivalent; a text column plus CHECK migrates identically on both.

**Alternatives considered**: Postgres-only (rejected by FR-048 — no additional services for the
default install); SQLite-only (spec explicitly recommends Postgres past ~1M entries).

## R5 — Full-text search: dialect-specific, behind one function

**Decision**: `db/search.py` exposes `search_condition(term)` and `index_document(...)`. SQLite uses
an FTS5 table; Postgres uses a `tsvector` column with a GIN index. The index is maintained by the
ingest writer inside the same transaction as the row it describes.

**Rationale**: FR-028 requires free text over titles and review text; SC-007 caps the first page at
1 s p95 over 1M entries, which `LIKE '%term%'` cannot deliver at that size. Maintaining the index in
application code rather than database triggers keeps one implementation of *when* to index and
avoids writing trigger DDL twice.

**Alternatives considered**: `LIKE` only (fails the budget — this is the "measured, not asserted"
rule in Constitution VI); Postgres `pg_trgm` (does not help SQLite); an external search service
(violates FR-048).

## R6 — Pagination: keyset cursors only

**Decision**: Opaque base64 cursor encoding the last row's sort key plus its `id` as a tiebreaker;
`(logged_at DESC, id DESC)` is the default order, with matching composite indexes for each permitted
`sort` value. `limit` capped server-side.

**Rationale**: FR-030 and SC-007's "far end no slower than the start". `OFFSET` degrades linearly and
the spec calls it a footgun explicitly. Including `id` in the key makes the order total, so no row is
skipped or repeated when timestamps tie — which they will, in bulk-imported history.

## R7 — Rating normalization

**Decision**: Linear scales use `round(100 * (raw - min) / (max - min))`. Ordinal scales carry an
explicit `value -> normalized` map in `rating_scales.labels`. Both raw value and scale id are stored
on every opinion; the normalized value is derived and recomputable from stored data.

**Rationale**: FR-003. Recomputability matters because a corrected scale definition must be
repairable without re-syncing — the same replay principle as R16.

## R8 — Creator resolution on the hot path

**Decision**: Per run, collect all credits in the batch, then: one indexed batch `SELECT` on
`creator_external_ids (namespace, value)` for asserted identifiers, one on
`creator_aliases (normalized, media_family)` for the rest, both against a per-run dict memo. Insert
misses in one statement. Never a query per credit.

**Rationale**: §5.8.2 — music providers credit artists on every track, so this runs at listen volume,
not work volume. The budget is ≥20,000 credit lookups/minute with no degradation as the creator table
grows, and it is benchmarked in M2 before AniList lands, exactly as the source design instructs.

**Alternatives considered**: per-credit lookup (O(n) round trips — the obvious way this feature
misses SC-008); an in-process LRU across runs (unnecessary once the per-run memo exists, and it would
have to be invalidated by merges and splits).

## R9 — HTTP client and host-enforced politeness

**Decision**: One `httpx.AsyncClient` per run, constructed by the host and handed to the provider in
`ProviderContext`. The host wraps it with: a token-bucket limiter whose rate is
`max(provider_declared, host_floor_for_acquisition_mode)`; a per-host `asyncio.Semaphore(1)` for
`scrapes` providers; retry on 5xx/429/transport with jitter; `Retry-After` compliance; ETag /
`If-Modified-Since` pass-through; and a fixed User-Agent naming the project, version, and contact URL.

**Rationale**: FR-043 requires these to be un-overridable by plugin code — so the limiter must live
in the wrapper the provider is *given*, and the provider must have no way to construct its own client
that the host does not control. The floor is a `max()`, never a plugin-supplied value.

**Consequence**: providers must not import `httpx` directly; an import-linter rule in CI enforces it,
because a review-only rule is the kind that eventually leaks.

## R10 — Image cache

**Decision**: `GET /media/image/{hash}` where hash is `sha256(source_url)`. On miss: fetch once, store
bytes content-addressed under `$AGGREGATO_DATA/images/ab/cd/<sha256-of-bytes>`, record the mapping,
serve with a long cache header. Fetch happens on first *request*, never during sync. Caching off →
placeholder response, never a redirect to the third-party host.

**Rationale**: FR-033. Content-addressing the *bytes* deduplicates the same artwork arriving from
several providers; keying the *endpoint* by URL hash avoids leaking source URLs into the UI. Lazy
fetch is what makes "an image host being down never fails a sync" true rather than aspirational.

## R11 — Frontend: Vue 3 SPA, and the deviation it creates

**Decision**: Vue 3 + Vite + TypeScript + vue-router. No state library — a `useApi` composable and
component-local state cover it; there is one user and no shared mutable client state worth a store.
Provider settings forms are rendered by one `SchemaForm.vue` from the provider's Pydantic JSON Schema
served by `GET /providers/{id}/config-schema`. Built in a Docker stage; the runtime image contains
only static assets served by the API process.

**Rationale**: Operator's explicit choice. It reinforces FR-031 — an SPA cannot reach the database or
a private endpoint even by accident, so "the UI consumes only the public API" becomes structural.
FR-039 is satisfied by ~120 lines of schema-driven form rendering, because provider config models are
flat scalars, enums, and secrets.

**Deviation, stated plainly**: the spec's Assumption says server-rendered specifically so a
self-hoster needs no Node toolchain. Building the image or running from source now requires Node.
Operators using `docker compose up` are unaffected. Recorded in plan.md Complexity Tracking; the spec
Assumption should be amended rather than quietly contradicted.

**Alternatives considered**: a JSON-Schema form library (heavier than the flat-schema renderer it
would replace); Pinia (no shared state to manage); Nuxt/SSR (reintroduces the Node requirement it was
meant to remove, with more machinery).

## R12 — Auth: bearer token, cookie session for the browser

**Decision**: Every request requires either `Authorization: Bearer <api.token>` or a session cookie.
`POST /auth/session` exchanges the token for an `HttpOnly; SameSite=Lax; Secure`-when-TLS cookie
holding a signed, expiring session id. Cookie-authenticated state-changing requests require a CSRF
token; bearer requests do not. Token rotation invalidates all sessions. Startup fails if `api.token`
is unset — the single fatal configuration error (FR-032, §8).

**Rationale**: FR-032 forbids the token appearing in URLs or page source, which rules out passing it
to a SPA bootstrap. Constant-time comparison on the token; sessions stored in the database so
rotation can actually invalidate them.

## R13 — Testing strategy

**Decision**:
1. **Conformance suite** (`tests/conformance/`) — a parametrized pytest module every bundled provider
   is registered into. It asserts: declared capabilities match observed behaviour; `normalize` is
   pure (called twice on the same fixture → identical output; no network via a blocked-socket
   fixture; no clock via a frozen-clock assertion); every `media_type` produced is in the enum; every
   `subject_ref` passes fixed-key validation; cursors round-trip; identifiers found in the fixture are
   all extracted. This is the contract test Constitution II mandates and the compliance proof FR-046
   requires.
2. **Contract tests** — responses validated against `contracts/openapi.yaml` (schemathesis-style
   validation of the served schema, plus explicit tests for RFC 9457 shape and cursor semantics).
3. **Integration tests** — one per user story, driving the real scheduler, real bundled providers, and
   recorded fixtures, over a temporary SQLite file.
4. **Unit tests** — resolution branches, retry classification, `subject_ref` validation, rating
   normalization (both kinds), family mapping, cursor codec, config precedence.
5. **Benchmarks** (`tests/bench/`) — the plan.md budget table, run against a generated 1M-entry
   fixture database. Lands in M2.

**Rationale**: Determinism is a constitutional requirement, so a blocked-socket fixture and an
injected clock are infrastructure, not test hygiene. Network access in a test is a failure, not a
slow test.

## R14 — Packaging

**Decision**: Multi-stage Dockerfile — Node stage builds `frontend/`, Python stage installs the
package (uv), final stage carries the venv, the built assets, and no toolchain. One `docker
compose.yml` with one volume. Two processes started by the image entrypoint (API + scheduler), the
scheduler being restartable independently.

**Rationale**: FR-048 and the deviation mitigation in R11. `docker compose pull` is documented as the
standard operator response to a `structure_changed` provider, per the source design's §12.7.

## R15 — Configuration precedence

**Decision**: Defaults ← YAML file ← `${ENV_VAR}` interpolation ← database overrides (UI-editable).
Secrets resolve from the environment at read time and are never written back to disk or returned by
the API. The API reports, per setting, whether it is file-pinned. Invalid provider config disables
that provider with a recorded error and never blocks startup.

**Rationale**: §8 and FR-052. "File-pinned" must be a field in the API response, or the UI cannot
explain why an edit does not stick.

## R16 — Normalization replay

**Decision**: `provider_items.raw_payload` plus `schema_version`. On startup, if a provider's declared
`schema_version` exceeds the max stored for it, a background replay job re-runs `normalize` over
stored payloads in batches and rewrites derived rows — no network. The same code path re-processes
`ingest_failures` and re-derives `role` from `role_raw` when the role vocabulary widens.

**Rationale**: FR-002 and SC-011. Because `normalize` is pure (R2), replay is just calling it again;
that purity is what the conformance suite tests, and this is what it buys.

## R17 — `subject_ref` validation

**Decision**: A Pydantic model with exactly six optional integer fields (`season`, `episode`, `track`,
`disc`, `chapter`, `volume`), `extra="forbid"`, positive integers only, at least one field set or the
value must be `null`. Validated in the parent process at the ingest boundary — never trusted from the
child.

**Rationale**: FR-007/FR-008 and §5.2.1's explicit "so it can't drift into a dumping ground". Parent-
side validation because the child runs plugin code.

## R18 — Aggregate defaults for sub-unit records

**Decision**: Every aggregate query (`/stats/*`, work-level score summaries) filters
`subject_ref IS NULL` unless `include_subunits=true`. Implemented once in a shared query builder, with
a unit test asserting the default direction explicitly.

**Rationale**: FR-007. The spec calls getting this backwards a silent corruption of every statistic a
per-episode-tracking user sees — so it gets a named test whose failure message says exactly that.

## R19 — Logging

**Decision**: Stdlib `logging` with a JSON formatter and a `contextvars`-backed run context
(`provider_id`, `run_id`, `lineage_id`) injected by a filter. `sync_runs.log_excerpt` captures the
tail of a failed run's records for display.

**Rationale**: FR-051 and §11 require structured logs and a run excerpt; a formatter plus a filter is
about 40 lines against a dependency. `structlog` is the upgrade path if binding gets painful — noted
rather than pre-adopted.

## R20 — Delete inference

**Decision**: Tombstones only for providers declaring `reports_deletes`. For everyone else, an
`infer_deletes` setting defaulting to **false**, per provider, ignored entirely unless the run was a
`full` fetch that completed successfully *and* passed the sanity threshold (R21).

**Rationale**: FR-024 and SC-014. Three independent conditions, because this is the failure mode that
destroys the archive, and a feed provider returning only recent items must never be able to reach the
deletion path.

## R21 — Sanity threshold

**Decision**: Per provider, store the previous run's item count for the same window. A run returning
fewer than a configurable proportion (default 50%) is marked `partial`, surfaced, and blocked from
delete inference.

**Rationale**: FR-026, and it is the guard that makes a broken scraper look like a broken scraper
rather than like an emptied history.
