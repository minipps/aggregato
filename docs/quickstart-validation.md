# Quickstart validation record

Validated 2026-07-30 from a fresh test data directory per test case. Every validation is offline:
the test fixture blocks socket access, so a passing result cannot have contacted a platform.
This is a dated validation record, not a current branch baseline; use the [CI workflow](../.github/workflows/ci.yml)
for the commands and results that gate a release.

| Quickstart section | Command / evidence | Result |
|---|---|---|
| Silent install, deletion, sub-unit, interrupted-sync guards | [Validation guide](validation.md) and its named tests | 31 passed across all user-story journeys |
| One-provider journey | `tests/integration/test_single_provider.py` | passed: enable → sync → browse → resync, with no duplicate archive rows |
| Cross-provider, failure, merge/split, import, and export journeys | [Validation guide](validation.md) | passed |
|  conformance | `uv run pytest tests/conformance/ -q` | 41 passed, 5 skipped (optional FTS-dependent cases) |
| Dependency boundary | `uv run lint-imports` | 3 contracts kept, 0 broken |
| Performance budget assertions | `uv run pytest tests/bench/ -q` | 6 passed |
| Backend quality suite | formatter, Ruff, mypy, full pytest | 523 passed, 5 skipped; zero formatter/lint/type errors |
| Frontend | `npm run type-check && npm run build` | passed |
| Clean runtime | Fresh Podman backend/frontend image builds, isolated volume/containers, authenticated `/api/v1/health` through the frontend | passed: migrations, API, worker, and SPA proxy started; health returned `200 {"status":"ok","providers":[]}` |

The quickstart previously called `pytest --benchmark-only`, but pytest-benchmark is not a project
dependency; that command could never run. It now invokes the repository's portable benchmark-budget
assertions directly.

## Container-runtime check

This workspace does not provide Docker Compose. Podman is installed, but neither a Docker Compose
plugin nor `podman-compose` is present, so `podman compose` cannot select a provider. The equivalent
single-service flow was therefore run directly against the same backend image and environment for the
previous validation. The two-service Compose path now builds `docker/Dockerfile` and
`docker/frontend.Dockerfile`; on a host with Compose, validate it with the commands in
`docs/validation.md`, including an authenticated health request through the frontend port.
