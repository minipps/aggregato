# Quickstart & Validation Guide: Aggregato

**Date**: 2026-07-29 | **Architecture**: [architecture.md](architecture.md) | **Contracts**: [openapi.yaml](contracts/openapi.yaml) · [provider-plugin.md](contracts/provider-plugin.md)

How to run the service and how to prove each user story works. Every check below is runnable; none
requires a real platform account except where explicitly marked **manual**.

---

## Prerequisites

| Purpose | Requirement |
|---|---|
| Running the release | Docker + Docker Compose only |
| Developing | Python 3.13, `uv`, Node 20+ (frontend build only — see the deviation note in [research.md](./research.md) ) |
| Tests | Nothing else. No database server, no network, no credentials |

---

## Run it

```bash
# Release path — what an operator does
export AGGREGATO_TOKEN=$(openssl rand -hex 32)
docker compose -f docker/compose.yml up

# Development path
uv sync
export AGGREGATO_TOKEN=dev-token AGGREGATO_DATA=./data
uv run alembic upgrade head          # also runs automatically at startup
uv run uvicorn aggregato.main:app --reload    # API process
uv run python -m aggregato.worker             # scheduler process, separate on purpose
cd frontend && npm install && npm run dev     # dev server proxies /api to :8000
```

`api.token` is the only fatal configuration error: startup fails without it, because there is no
unauthenticated mode . A missing or invalid *provider* configuration never blocks startup —
that provider is marked `misconfigured` and everything else runs .

---

## Validate the checks that guard the whole design

Run these first; they are the ones whose failure means something silently destructive:

```bash
#  /  — no inferred deletion for a provider that does not report deletes
uv run pytest tests/integration/test_no_inferred_deletes.py -v

#  — sub-unit records excluded from aggregates BY DEFAULT
uv run pytest tests/unit/test_subunit_aggregate_default.py -v

#  /  — interrupted syncs produce neither loss nor duplication
uv run pytest tests/integration/test_idempotent_interrupted_sync.py -v

#  — a fresh install makes zero outbound requests
uv run pytest tests/integration/test_fresh_install_is_silent.py -v
```

---

## Per-story validation

###  — One platform's history, local and browsable

```bash
uv run pytest tests/integration/test_single_provider.py -v
```

Drives the bundled `fixture` provider end to end: enable → scheduler picks it up → child process
fetches → parent ingests → `GET /api/v1/entries` returns the rows.

Expected: entry count matches the fixture exactly; every entry carries `logged_precision`; every
opinion returns `raw`, `scale_id`, and `normalized`; a second run writes nothing new.

**Manual check** (real platform, one time): enable a real provider, wait for the first sync, and spot
check ten entries against the platform's own UI, including one logged with a month-only date.

###  — Two platforms, one log

```bash
uv run pytest tests/integration/test_cross_provider_identity.py -v
```

Two fixture providers with deliberately overlapping data: one pair sharing an identifier (must unify
silently), one pair matching only on title and year (must unify only when unambiguous), one
ambiguous pair (must land in `resolution-queue`, never be guessed), and one pair of same-titled
different works (must stay separate).

Expected: `GET /works/{id}` shows both providers' opinions on the unified item; the ambiguous pair
appears in `GET /resolution-queue` with a reason per candidate; an operator decision survives a
subsequent resync of both providers.

###  — Sync the operator can trust

```bash
uv run pytest tests/integration/test_failure_matrix.py -v
```

One parametrized case per `error_class`. Assert per case: the resulting `sync_runs` row, whether a
retry was scheduled and at what interval, the resulting provider status, and that the message names
an action .

| Injected failure | Expected |
|---|---|
| transport / 5xx | retried in-run, then ladder 1m → 5m → 15m → 1h, one row per attempt, shared `lineage_id` |
| `rate_limit` with `Retry-After` | honoured; effective interval lengthened for the session |
| `auth` | **no retry**, `degraded` immediately, message names credentials |
| `blocked` | **no retry**, `degraded` immediately, message names the block |
| `structure_changed` | **no retry**, message says the provider needs updating |
| mid-run failure after 2 pages | status `partial`, cursor at last checkpoint, retry resumes there |
| one permanently-failing record | that record in `ingest_failures` with its payload, run completes, `items_failed = 1` |
| provider child killed (SIGKILL) | run `failed`, API still serving, other provider's sync unaffected |
| provider child hangs | killed at the wall-clock timeout, classified, ladder applies |
| provider config invalid | `misconfigured`, service starts, other providers sync |

###  — Fixing identity by hand

```bash
uv run pytest tests/integration/test_merge_split.py -v
```

Expected: merging two works combines entries, opinions, and identifiers with nothing lost; splitting a
creator moves exactly the selected credits; each credit exposes `link_confidence` so name-joined
credits are distinguishable from identifier-joined ones; a cross-family duplicate appears as a
`suggestion_kind` item rather than an auto-merge; every operation returns an `undo_url` that works.

**Manual check**: clear ten queue items using only the keyboard and time it —  budget is under
10 seconds per decision.

###  — Platforms with no API

```bash
uv run pytest tests/integration/test_us5_file_import.py -v
```

Expected: `POST /providers/goodreads/import` with a recorded CSV ingests entries with ISBN
identifiers; re-uploading the same file adds nothing; an unrelated file is rejected with a problem
document and leaves the archive unchanged.

###  — Adding support for a new platform

```bash
uv run pytest tests/conformance/ -v          # every bundled provider, all 9 conformance groups
uv run lint-imports                          # provider tree cannot reach db or httpx
```

Expected: a new provider passes conformance without core changes; `GET /providers/{id}/config-schema`
renders in `SchemaForm.vue` with no per-provider frontend code; bumping `schema_version` replays the
archive from stored payloads with no network ; a scraping provider is labelled, rate-floored,
and refuses to run without fixtures.

###  — Owning the archive

```bash
uv run pytest tests/integration/test_export_restore.py -v
```

Expected: `GET /export` produces one archive with no secrets in it; restoring into an empty instance
yields an identical, browsable archive with zero syncs; the storage view reports usage separately for
raw payload retention and image cache, each independently switchable.

---

## Performance budgets (performance guidance)

```bash
uv run pytest -m bench tests/bench/ -q              # portable benchmark-budget assertions
uv run python -m tests.bench.seed --entries 1000000   # generates the fixture database
```

| Check | Budget | Fails if |
|---|---|---|
| `test_first_and_deep_keyset_pages_fit_baseline` | first page < 200 ms and deep page < 300 ms on the seeded 20k sample | pagination indexes or keyset predicates regress |
| `test_creator_resolution_meets_budget_and_stays_flat` | ≥ 20,000 lookups/min at 200 and 2,000 creators; large stays at least half the small rate | a per-credit query or table-size regression appears |
| `test_live_entry_keyset_sorts_use_composite_indexes` | both timestamp sorts use their composite active-entry index | a query plan falls back to a table scan |
| `test_live_score_facts_use_provider_item_composite_index` | score aggregation uses the provider-item/rating index | score filtering loses its indexed path |

The operator harness can seed a 1M-entry database for larger measurements. Record any resulting
baseline in `tests/bench/baseline.json`; a change worsening a recorded metric by more than 10% needs
a justification in the architecture documentation, per the engineering guidance.

---

## Quality gates before merge

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy aggregato/   # zero warnings
uv run pytest                                                                   # all suites
uv run lint-imports                                                             # decoupling contract
cd frontend && npm run type-check && npm run test:unit
```

All four must be clean. Warnings are errors (code-quality guidance), and the import contract is a merge gate
rather than a convention (decoupling guidance).
