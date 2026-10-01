# Architecture: Aggregato — Self-Hosted Media Log Aggregator

See [requirements.md](requirements.md) for product behavior and acceptance criteria. Performance figures
below are targets; the current benchmark suite does not establish every target at its stated scale.

## Summary

Aggregato is a single-user, self-hosted service that collects media logs from configured platforms
into a local database and serves them through an HTTP API and web UI. Bundled and drop-in providers
implement the same three-method contract. The host owns scheduling, retries, rate limits, ingest
validation, identity resolution, and migrations.

FastAPI serves the API. A separate worker polls the database-backed due queue, and a short-lived
child process runs each provider operation. The parent validates child output and performs database
writes. The child has no database engine and timeouts contain many crashes and hangs; this process
boundary is not an OS sandbox, and provider code still has the service's filesystem and network
permissions.

Storage uses SQLAlchemy Core over SQLite (WAL) or PostgreSQL, with Alembic migrations. The Vue SPA
uses the documented API; authentication and authorization are enforced by the server. Compose runs
the API/worker image and the Nginx frontend image.

## Technical Context

**Language**: Python 3.13 (host, API, providers); TypeScript (frontend)

**Primary Dependencies**: FastAPI + uvicorn; Pydantic v2 (provider config models, normalized
payloads, settings); SQLAlchemy 2.0 Core (async) + Alembic; aiosqlite / asyncpg; httpx; PyYAML;
Vue 3 + Vite + vue-router. Standard library for scheduling, process isolation, and structured
logging — see [research.md](research.md).

**Storage**: SQLite (WAL) by default at `$AGGREGATO_DATA/aggregato.db`; Postgres optional via the
same URL setting. No Postgres-only column types.

**Testing**: pytest and pytest-asyncio; httpx `ASGITransport` for API tests; a shared provider
conformance suite; recorded fixtures and socket-blocked tests. Vitest runs the frontend unit tests.

**Deployment target**: Linux, two Docker images, one volume, `docker compose up`. Single-board-class
hardware is a target; the declared performance budgets have not been demonstrated there.

**Project Type**: Web service (async API + scheduler worker) with an SPA frontend and an in-repo
plugin tree.

## Performance budgets

These are product targets, not current measurements. See `tests/bench/` for the checks that exist;
they do not yet prove every latency and throughput target below at the stated archive size.

| Metric | Budget |
|---|---|
| First page of filtered `/entries` at 1M entries | p95 < 1000 ms |
| Deep page (far end of 1M entries) | within 20% of first-page latency |
| Sustained ingest throughput | ≥ 50,000 entries/hour on 4-core ARM / 2 GB RAM |
| Ingest throughput at 1M rows vs at 10k rows | no measurable degradation (±10%) |
| Creator resolution | ≥ 20,000 credit lookups/minute, batched, O(1) amortized per credit |
| Clean start to first browsable sync | < 15 min wall clock, operator time < 5 min |
| Fresh install outbound requests | exactly 0 |
| Now-playing acquisition cadence | each enabled capable provider is checked every 15 s on success |
| Now-playing API polling | WebSocket reads durable state every 500 ms |
| Now-playing visibility | provider changes reach the WebSocket within 16 s in normal operation |
| Now-playing freshness | items checked more than 45 s ago are omitted |

The [local publication measurements](publication-review/performance.md) use a synthetic million-entry
SQLite archive. First/deep-page API p95 measured 185/186 ms after fixing the keyset range predicate.
Three short writer bursts per archive size measured a 15.2% lower median rate at 1M rows than at
10k, outside the ±10% target. The concentrated seed and overlapping trial ranges limit that
comparison; the scaling target needs representative, sustained measurements before it can be
claimed. The 4-core ARM budget remains unmeasured.

**Constraints**: no outbound request to a non-platform host except for images at platform-supplied
URLs; authentication is required by default, with an explicit read-only public mode for GET/HEAD.
Provider output is validated before writes. Child processes and timeouts limit the effect of provider
failures, but unreviewed Python providers are not sandboxed.

**Long-term scale target**: 1 user, about 10 bundled providers, 1M+ entries, 100k+ works, and
200k+ creators. These figures describe intended scope, not a measured capacity limit.

## Engineering checks

The architecture satisfies the engineering guidance in [AGENTS.md](../AGENTS.md): strict quality
checks, deterministic tests and provider conformance, consistent UI behaviour, inward dependency
direction, and performance targets. The frontend is a built SPA.

## Project Structure

### Documentation

```text
docs/
├── requirements.md      # Product requirements and acceptance scenarios
├── architecture.md      # This file
├── research.md          # Technical decisions with rationale
├── data-model.md        # Tables, constraints, and state transitions
├── validation.md        # How to run and validate product journeys
├── contracts/
│   ├── openapi.yaml     # The public HTTP contract
│   └── provider-plugin.md  # Plugin contract and conformance obligations
└── writing-a-provider.md # Practical provider authoring guide
```

### Source Code (repository root)

```text
aggregato/
├── main.py                     # FastAPI app factory; API process entrypoint
├── worker.py                   # scheduler process entrypoint (separate process from the API)
├── config.py                   # YAML + ${ENV} precedence, explicit overrides, file-pinned markers
├── logging.py                  # stdlib logging + JSON formatter, run-scoped context
├── domain/
│   ├── enums.py                # MediaType, MediaFamily, Role, EntryKind, Capability, Acquisition,
│   │                           #   Confidence, ErrorClass, RunStatus, RunPhase — closed vocabularies
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
│   ├── scheduler.py            # due-queue poll loop, singleton ownership, concurrency cap
│   ├── now_playing.py          # transient playback monitor and durable state transitions
│   ├── runner.py               # spawns and supervises one child per run; wall-clock kill
│   ├── child.py                # child entrypoint: fetch + normalize -> JSON lines on a pipe
│   ├── jobs.py                 # typed import/replay leases and shared lifecycle transitions
│   ├── dispatch.py              # bounded replay, ingest, outcome release
│   ├── retry.py                # backoff ladder, lineage, degraded state
│   └── errors.py               # ErrorClass mapping and the never-retry set
├── providers/
│   ├── base.py                 # Provider Protocol, ProviderContext, RatingScale
│   ├── registry.py             # static-manifest discovery; selected drop-ins are labelled unreviewed
│   ├── http.py                 # rate limiting, politeness floors, UA, Retry-After, ETag
│   ├── fixture/                # offline provider used by tests
│   ├── listenbrainz/
│   ├── anilist/
│   ├── goodreads/              # public RSS feed
│   └── letterboxd/             # public RSS feed
├── api/
│   ├── deps.py                 # bearer auth, session cookie, CSRF
│   ├── errors.py               # RFC 9457 problem+json
│   ├── pagination.py           # keyset cursor codec
│   └── routes/                 # works, entries, opinions, creators, providers, stats,
│                               #   resolution, failures, images, health, export, auth,
│                               #   now-playing websocket
├── images/cache.py             # content-addressed lazy fetch and store
└── export.py                   # SQLite archive creation and restore helper

frontend/                       # Vue 3 + Vite SPA; built in docker/frontend.Dockerfile
├── src/api/                    # fetch wrapper and handwritten OpenAPI contract types
├── src/views/                  # Dashboard, Log, Work, Creators, Creator, Providers,
│                               #   SyncHistory, Resolution, Stats, Settings, Login
└── src/components/SchemaForm.vue  # renders a provider's JSON Schema as a settings form

tests/
├── conformance/                # the shared provider conformance suite (contract tests)
├── contract/                   # API responses vs contracts/openapi.yaml
├── unit/
├── bench/                      # performance checks
└── fixtures/<provider>/        # recorded payloads; the only data any test touches

docker/                         # backend/frontend multi-stage images, Nginx config, Compose files
CONTRIBUTING.md                 # acquisition and scraping policy
```

### Live sync observability

The API process and scheduler do not share memory, so live status is durable. The parent process
updates the open `sync_runs` row as it validates child messages, flushes checkpoints, and completes
ingest. `GET /api/v1/sync/status` exposes the queue and latest run snapshot; the authenticated
`/api/v1/ws/sync` websocket polls those same rows and sends a new snapshot only when it changes.
The SPA uses that stream in Dashboard, Providers, and Sync History, with the HTTP endpoint as a
reconnect fallback. A provider total may be unknown, in which case the UI shows an indeterminate
progress state rather than inventing a percentage.

Current playback uses the same durable-boundary principle without entering the history pipeline. The
scheduler process runs a separate now-playing monitor beside normal sync scheduling; it discovers
only static manifests declaring `now_playing`, starts the existing isolated child for one result
(maximum three concurrent children, 30-second wall-clock limit), and stores at most one
parent-validated item per provider in `provider_state`. The API process reads that
state through authenticated `/api/v1/ws/now-playing`, sends a complete initial snapshot, then polls
the database every 500 ms and sends only semantic changes. It omits disabled or failed providers and
items whose last completed check is older than 45 seconds. No playback event, progress, history, or
HTTP fallback is created by this path.

## Tradeoffs

The database is also the worker's durable schedule, so a single-host installation does not need a
message broker. A non-blocking `flock` on a SQLite sidecar or a PostgreSQL session advisory lock
enforces one worker owner per database. The worker acquires this process-lifetime lock before
migrations and interrupted-run recovery; it is not a durable multi-host lease. Shutdown stops new
admission, drains active syncs for up to three seconds, then cancels and reaps remaining children.
The now-playing monitor is stopped at the same time, and the database engine is disposed last. A
lost PostgreSQL session releases its advisory lock. Each provider operation runs in a fresh child
process; this bounds ordinary crashes and hangs while keeping all database writes in the parent, but
it does not sandbox provider code. The frontend is a built Vue SPA: Docker users need no Node
installation, while building it from source does.

SQLite is the default. PostgreSQL is supported through the same storage interface, and full-text
search uses SQLite FTS5 or PostgreSQL `tsvector`. Those dual implementations keep search indexed
without adding another service. Declared performance budgets remain targets until measurements at
their stated data sizes are recorded.
