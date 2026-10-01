# Development and validation

Run commands from the repository root unless noted. The API requires a token even when public
read-only access is enabled.

## Run locally

Install Python dependencies and start the API and scheduler in separate terminals:

```bash
uv sync --all-extras --dev
export AGGREGATO_TOKEN=dev-token AGGREGATO_DATA=./data
uv run uvicorn aggregato.main:app --reload
```

```bash
export AGGREGATO_TOKEN=dev-token AGGREGATO_DATA=./data
uv run python -m aggregato.worker
```

For frontend work, install Node 22 and run from `frontend/`:

```bash
npm ci
npm run dev
```

Vite proxies `/api` to the API on port 8000.

## Checks

These are the Python and frontend checks run in CI:

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run lint-imports
uv run pytest -m "not bench"
```

```bash
cd frontend
npm ci
npm run type-check
npm run test:unit
npm run build
```

To check one Python test, pass its path to pytest. Provider contract checks can be run with
`uv run pytest tests/conformance`.

## Optional database checks

CI checks PostgreSQL separately from the socket-blocked SQLite test suite. To run the smoke check
against a disposable PostgreSQL 16 database:

```bash
AGGREGATO_POSTGRES_URL=postgresql+asyncpg://aggregato:aggregato@127.0.0.1:5432/aggregato \
  uv run python scripts/postgres_smoke.py
```

The benchmark tests and archive harness use synthetic local SQLite data; they do not establish
performance on other machines or workloads:

```bash
uv run pytest -m bench tests/bench -q
uv run python scripts/benchmark_archive.py
```

The archive harness reports local query and writer measurements and leaves its generated databases
in a temporary directory. Pass `--workdir PATH` to choose where they are stored.
