# Quickstart validation record

Validated 2026-07-30 from a fresh test data directory per test case. Every validation is offline:
the test fixture blocks socket access, so a passing result cannot have contacted a platform.

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
| Clean runtime | Fresh Podman image build, isolated volume/container, authenticated `/api/v1/health` | passed: migrations, API, and worker started; health returned `200 {"status":"ok","providers":[]}` |

The quickstart previously called `pytest --benchmark-only`, but pytest-benchmark is not a project
dependency; that command could never run. It now invokes the repository's portable benchmark-budget
assertions directly.

## Container-runtime check

This workspace does not provide Docker Compose. Podman is installed, but neither a Docker Compose
plugin nor `podman-compose` is present, so `podman compose` cannot select a provider. The equivalent
single-service flow was therefore run directly against the same Dockerfile image and environment:
fresh base images were pulled, the production image built, an isolated container started with a
fresh `/data` volume and token, migrations ran, and authenticated health returned 200. The compose
wrapper itself remains a deployment-host concern; its service, environment, port, volume, and
healthcheck match the validated image runtime.
