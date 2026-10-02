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

For frontend work, install Node 26 (CI and Docker use 26.10.0) and run from `frontend/`:

```bash
npm install --global npm@12.2.0
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
npm run lint
npm run type-check
npm run test:unit
npm run build
```

Frontend linting uses [E18E's recommended rules](https://github.com/e18e/eslint-plugin) for
JavaScript, TypeScript, and Vue scripts, including tests. It also checks `package.json` for
replaceable dependencies. Run `npm run lint:fix` to apply automatic fixes. TypeScript stays on
6.0.3 because the current TypeScript ESLint parser supports versions below 6.1; upgrade it when
the parser and Vue tooling support TypeScript 7. The parser's
[supported dependency versions](https://typescript-eslint.io/users/dependency-versions/) explain
this constraint. ES2023 built-ins follow Vite's default browser target.

For dependency maintenance, run these from `frontend/`:

```bash
npm outdated
npm audit
npx @e18e/cli@0.7.0 analyze --json
npx @e18e/cli@0.7.0 migrate --all --dry-run --include 'src/**/*.{ts,js,vue}'
```

The E18E adoption ran package migrations and all web-feature codemods, extracting Vue scripts
before trying the latter. No direct dependency needed replacement. Reviewed `Object.hasOwn` and
array `toSorted` conversions were applied; the codemod's invalid `Set.toSorted` conversion was
rejected. The CLI reports duplicate transitive versions required by incompatible dependency
ranges; these are retained rather than forced through overrides. Its advisory warnings make
`analyze` exit nonzero; `npm run lint` is the enforced CI gate.

The production build's total individually gzipped output measured 79,799 bytes before this
refresh and 79,858 bytes after it (+0.07%), within the 10% regression limit. Measure after
`npm run build` with:

```bash
python - <<'PY'
import gzip
from pathlib import Path

print(sum(len(gzip.compress(p.read_bytes())) for p in Path('dist').rglob('*') if p.is_file()))
PY
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
