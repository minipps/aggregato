# Validation guide

Commands are run from the repository root unless noted. The [CI workflow](../.github/workflows/ci.yml)
uses Python 3.13 and Node 22.23.2. The Python test suite uses temporary SQLite databases and blocks
socket access; it needs no provider accounts or network. PostgreSQL smoke checks run separately.

## Run locally

```bash
uv sync --all-extras --dev
```

Start the API and scheduler in separate terminals, exporting the same settings in each:

```bash
export AGGREGATO_TOKEN=dev-token AGGREGATO_DATA=./data
uv run uvicorn aggregato.main:app --reload
```

```bash
export AGGREGATO_TOKEN=dev-token AGGREGATO_DATA=./data
uv run python -m aggregato.worker
```

For frontend development, use Node 22.23.2 and run `npm ci && npm run dev` from `frontend/`; Vite
proxies `/api` to the API on port 8000. The token is required even when public read-only access is
enabled.

## Python checks

These are the CI quality gates. The regular suite includes provider conformance and excludes the
separate benchmark marker.

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
uv run pytest -m "not bench"
```

Focused regression suites:

```bash
uv run pytest \
  tests/integration/test_fresh_install_is_silent.py \
  tests/integration/test_no_inferred_deletes.py \
  tests/integration/test_idempotent_interrupted_sync.py \
  tests/integration/test_replay_atomicity.py \
  tests/integration/test_failure_matrix.py

uv run pytest tests/conformance -v
uv run pytest tests/unit/test_now_playing.py \
  tests/integration/test_now_playing_end_to_end.py \
  tests/contract/test_now_playing_websocket.py
uv run pytest tests/integration/test_cross_provider_identity.py \
  tests/integration/test_merge_split.py \
  tests/integration/test_us5_file_import.py \
  tests/integration/test_export_restore.py
```

The [provider contract](contracts/provider-plugin.md) describes what conformance covers: declared
capabilities, deterministic normalization, vocabularies and identifiers, cursor behavior where
supported, credential checks, configuration schemas, optional now-playing, and scraping fixtures.
The now-playing tests cover monitor behavior, restart persistence, and authenticated WebSocket
snapshots, including ordering, freshness, and local image paths. Export/restore tests check
credential removal, access control, restore and storage behavior; a restored archive is checked for
browsable entries without a sync, not byte-for-byte identity with its source.

## Frontend checks

Run from `frontend/` with Node 22.23.2:

```bash
npm ci
npm run type-check
npm run test:unit
npm run build
```

## PostgreSQL and benchmarks

CI runs the PostgreSQL smoke check against a disposable PostgreSQL 16 service. To run it locally,
use a disposable PostgreSQL 16 database and the CI connection setting:

```bash
AGGREGATO_POSTGRES_URL=postgresql+asyncpg://aggregato:aggregato@127.0.0.1:5432/aggregato \
  uv run python scripts/postgres_smoke.py
```

The benchmark harness is separate from the ordinary suite:

```bash
uv run pytest -m bench tests/bench -q
```

These checks exercise query plans, a far-end keyset sample, and creator resolution. The declared
performance targets in [architecture.md](architecture.md) remain targets; the current benchmark
suite does not establish every target at its stated scale.
