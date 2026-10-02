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
uv run ruff format --check . && uv run ruff check . && uv run ty check
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

## Python dependency inventory

The development group includes the read-only [python-e18e CLI](https://github.com/python-e18e/cli),
pinned to a Git commit. Its replacement catalog is a separate input; without it, the scanner only
reports workflow modernization opportunities. Run a reproducible scan with the reviewed catalog:

```bash
curl -fsSL https://raw.githubusercontent.com/python-e18e/module-replacements/e917d4af6a18cf1049479d35c4fdd4349800f0d2/replacements.json \
  -o /tmp/aggregato-replacements.json
uv run python-e18e scan . --replacements /tmp/aggregato-replacements.json
```

The initial scan identified two declared dependency candidates:

| Candidate | Decision |
|---|---|
| mypy → ty | Applied to backend configuration, suppressions, and CI. Mypy passed before migration; Ruff retains parameter and return annotation requirements. Ty rules are not a one-to-one replacement for mypy strict, unreachable, or redundant-expression checks; see [Astral's migration guide](https://docs.astral.sh/ty/coming-from-mypy-or-pyright/). |
| httpx → httpx2 | Applied to the host client, image cache, error classification, test transports, benchmark harness, and import contract. HTTPX2 verifies outbound HTTPS using the [operating system trust store](https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md#230-june-1st-2026); Nginx handles incoming TLS separately. |

After both migrations, the same catalog scan reports no migration opportunities.

The CLI has no runtime dependencies and stays in the development group. Scans are advisory; they do
not edit code or inspect dependency usage, and downloading the catalog requires network access.

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
