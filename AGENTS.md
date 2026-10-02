# AGENTS.md

Working guidance for contributors and coding agents.

## Commands

```bash
uv sync --all-extras --dev
export AGGREGATO_TOKEN=dev-token AGGREGATO_DATA=./data   # no unauthenticated mode; startup fails without a token

uv run uvicorn aggregato.main:app --reload   # API process
uv run python -m aggregato.worker            # scheduler process — separate on purpose
cd frontend && npm install && npm run dev    # Vite dev server proxies /api to :8000
```

### Quality gates

All must be clean before anything merges. Warnings are errors.

```bash
uv run ruff format --check . && uv run ruff check . && uv run ty check
uv run lint-imports          # the decoupling contract — a merge gate, not a convention
uv run pytest                # CI runs `-m "not bench"`; no test may touch the network
cd frontend && npm run type-check && npm run test:unit
```

Single test / subset:

```bash
uv run pytest tests/unit/test_writer_idempotency.py::test_resync_writes_nothing
uv run pytest tests/conformance          # every provider must pass this
uv run pytest -m bench                   # the declared performance budgets (docs/architecture.md)
cd frontend && npm run test:unit -- src/components/__tests__/SchemaForm.spec.ts
```

Migrations auto-apply at API startup (`aggregato/db/migrate.py`); `uv run alembic upgrade head` to run
them alone. Release: `scripts/release.sh <version>` opens a version-bump PR; after merging and
updating `main`, `scripts/release.sh --tag <version>` starts release CI. Publishing requires approval
by `@minipps` in the `release` environment.

## Architecture

One deployable, **two long-lived processes plus one short-lived child per sync run**. Each boundary
exists to make a requirement physically true rather than reviewed:

- **API** (`aggregato/main.py`) — FastAPI under `/api/v1`, serves the built SPA at everything else.
  Never runs syncs; `import-linter` forbids `aggregato.api -> aggregato.sync`.
- **Scheduler** (`aggregato/worker.py` → `sync/scheduler.py` → `sync/dispatch.py`) — polls a
  database-backed due-queue, claims a provider, and calls `dispatch`, which owns the run:
  open `sync_runs` row → replay stale payloads if `schema_version` advanced → `execute_run` →
  ingest → close run → reschedule via the retry ladder.
- **Child per run** (`sync/runner.py` spawns `sync/child.py`) — imports exactly **one** provider,
  has **no database engine**, streams JSON-lines messages (`sync/protocol.py`) on stdout while
  logging to stderr. The parent validates every line, kills on wall clock, and performs all writes.
  This single mechanism delivers crash/hang containment *and* "a provider never gets a DB handle".

Dependency direction is enforced by `[tool.importlinter]` in `pyproject.toml`:
`api | sync` → `ingest` → `db` → `domain`, plus `aggregato.providers` may not import
`aggregato.db`, `aggregato.ingest`, or `httpx2` (one exemption: `providers/http.py`, which *is* the
host-owned client).

Storage is SQLAlchemy **Core** only — no ORM, no session, no repository layer; a `Connection` is the
interface writers and readers take (`db/engine.py`). SQLite (WAL) by default, Postgres via the same
URL setting, so no dialect-specific column types. `db/search.py` is the one justified two-impl
interface (FTS5 vs `tsvector`).

`ingest/writer.py` is the **only** thing that writes ingested data, and it validates in the parent
process — never trusting the child. Records that fail validation become `ingest_failures` rows with
their payloads, and the run continues.

## Providers

A provider is any object satisfying the `Provider` Protocol in `providers/base.py`: a module-level
object named `provider` in a package under `aggregato/providers/<id>/` (or a drop-in
`AGGREGATO_PROVIDER_DIR`, labelled *unreviewed* in the UI). Three methods: `fetch`, `normalize`,
`check`. The core knows no provider's name; `providers/registry.py` only lists what is on disk, and
discovery must be side-effect free — a fresh install makes exactly zero outbound requests.

Non-negotiable, and the conformance suite checks them:

- `normalize` is **pure** — no network, no clock, no randomness, no storage. Called twice on one
  fixture it must produce identical output, because normalization replay depends on it.
- A provider never constructs an HTTP client. `ctx.http` enforces
  `max(declared_rate, floor_for_acquisition_mode)`, one in-flight request per host for scrapers, and
  `Retry-After`. The floor cannot be raised from provider code.
- Closed vocabularies: a provider may not invent a `media_type`, `role`, or `subject_ref` key
  (six keys, positive integers only — `domain/subject_ref.py`).
- `logged_precision` is required with no default; `role_raw` carries the platform's own word
  verbatim; every identifier in a payload is extracted, including ones Aggregato has no use for
  (it never enriches from third-party metadata, so this is the main lever on match quality).
- Never report success on a structural failure — raise `StructureChangedError`. Silence plus delete
  inference is how an archive gets erased.
- **No CAPTCHA or anti-bot circumvention, in any form.** Raise `BlockedError`; the provider goes
  `degraded`. Read `CONTRIBUTING.md` (acquisition hierarchy + scraping policy) before writing one —
  both are review gates.

Bump `schema_version` whenever `normalize` changes how retained payloads map to rows; the next run
replays stored payloads through the child before fetching.

## Invariants worth knowing before you edit

- **Idempotency**: unique constraint + `ON CONFLICT`, never a pre-`SELECT`. For entries with no
  platform event id the writer dedupes on `(provider_item_id, kind, logged_at, subject_ref)` —
  without that fallback every resync of a feed would duplicate its whole history.
- **Nothing is destroyed**: deletes are `deleted_at` tombstones. Inferred deletes require a
  successful `full` run, an opt-in config flag, a provider lacking `reports_deletes`, and a sanity
  check on window size (`sync/sanity.py`).
- **Cursors** advance only to the last checkpoint the child actually *flushed*, never to where the
  child claimed it reached. A failure after a checkpoint is `partial`, and its records are still
  written — discarding them would turn a mid-run failure into data loss.
- **Ratings** store the raw value plus its scale id; the 0–100 normalization is derived and
  recomputable (`domain/ratings.py`). Ordinal scales carry an explicit label→value map, never
  interpolation. A normalized value is comparable within a scale, not across platforms.
- **Pagination** is keyset only — no `OFFSET` anywhere. The cursor carries `(sort_value, id)`
  because `id` is what makes the order total; a bad cursor is a 400, never a silent page one.
  NULL sort values sort last in both directions, which callers must mirror in `ORDER BY`.
- **Secrets** are resolved from `${VAR}` at read time, never written back, never returned by the
  API. Config precedence is defaults ← YAML ← `${ENV}` ← database overrides (`config.py`); anything
  set in the file is reported as `file_pinned` so the UI can explain why an edit won't stick. A
  broken *provider* block disables that provider only; a missing `api.token` is the one fatal error.
- **Auth** (`api/deps.py`): bearer token or DB-backed session cookie, `hmac.compare_digest`,
  HMAC-derived double-submit CSRF for cookie writes. `api.readonly_token` is bearer-only and
  method-gated — refusal is server-side, not hidden buttons. **CORS** is off unless
  `api.cors_origins` lists an origin: the SPA is same-origin in both prod and dev, so no browser
  preflights, and `OPTIONS` is a 405 by design. `*` is refused — credentials are enabled.
- **Tests block sockets** (autouse fixture in `tests/conftest.py`), including loopback; use
  recorded fixtures under `tests/fixtures/<provider_id>/` and httpx2 `ASGITransport`. `Clock` is
  injected — no test sleeps.
- **Migrations** are forward-only. A SQLite table rebuild turns foreign keys off around itself, and
  a migration needs a test that seeds rows at the previous revision and asserts they survive. Never
  edit an already-applied revision.

## Canonical documentation

Consult the relevant document before changing its subject. Keep code comments explanatory; do not
introduce external requirement identifiers.

| Document | Authority over |
|---|---|
| `docs/architecture.md` | Architecture, stack, and declared performance budgets |
| `docs/data-model.md` | Tables, constraints, state transitions, and migration rules |
| `docs/contracts/provider-plugin.md` | The plugin contract |
| `docs/contracts/openapi.yaml` | The HTTP contract the SPA consumes, and the only one |
| `CONTRIBUTING.md` | Acquisition hierarchy, scraping policy, PR checklist |
| `docs/writing-a-provider.md` | Practical path to a new provider |
| `docs/roadmap.md` | Pending release checks and future development work |

This repo is indexed by CodeGraph (`.codegraph/`) — prefer `codegraph_explore` over grep/read loops.

## Engineering principles and review gates

- Keep code readable, remove dead code, and avoid speculative abstractions. Public functions,
  types, and plugin contracts document their purpose, inputs, and failure modes.
- Every behavioural change has a deterministic automated test; bug fixes include a regression test.
  Tests never use the network, wall-clock sleeps, execution order, or implicit time/randomness.
- Keep the UI consistent and accessible: semantic structure, keyboard access, text alternatives,
  and shared conventions for dates, ordering, pagination, and empty/loading/error states.
- Dependencies point inward. Use explicit contracts rather than globals; cyclic dependencies and
  core dependencies on individual providers are forbidden.
- New sources, transforms, and outputs are plugins. Contract changes require a documented migration
  path and conformance-suite updates. A plugin failure must remain isolated.
- Declare and measure performance budgets before implementation. A recorded metric may not regress
  by more than 10% without a documented justification.

Before merging, run the quality gates above, pass provider conformance tests for every bundled
provider, preserve dependency boundaries, and justify any new dependency against the standard
library, platform facilities, and installed packages.
