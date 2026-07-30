# Quickstart validation record

Validated 2026-07-30 from a fresh test data directory per test case. Every validation is offline:
the test fixture blocks socket access, so a passing result cannot have contacted a platform.

| Quickstart section | Command / evidence | Result |
|---|---|---|
| Silent install, deletion, sub-unit, interrupted-sync guards | Named validation tests in `quickstart.md` | 31 passed across all user-story journeys |
| US1 | `tests/integration/test_us1_single_provider.py` | passed: enable → sync → browse → resync, with no duplicate archive rows |
| US2–US5 and US7 | Named integration tests in `quickstart.md` | passed |
| US6 conformance | `uv run pytest tests/conformance/ -q` | 41 passed, 5 skipped (optional FTS-dependent cases) |
| Dependency boundary | `uv run lint-imports` | 3 contracts kept, 0 broken |
| Performance budget assertions | `uv run pytest tests/bench/ -q` | 6 passed |
| Backend quality suite | formatter, Ruff, mypy, full pytest | recorded in the final Phase 10 audit |
| Frontend | `npm run type-check && npm run build` | passed |

The quickstart previously called `pytest --benchmark-only`, but pytest-benchmark is not a project
dependency; that command could never run. It now invokes the repository's portable benchmark-budget
assertions directly.

## Container-runtime check

This workspace does not provide Docker Compose. Podman is installed, but neither a Docker Compose
plugin nor `podman-compose` is present, so `podman compose` cannot select a provider. The compose
file therefore remains unexecuted in this environment. Before release, run the documented
`docker compose -f docker/compose.yml up` flow on a host with Docker Compose v2 and add its output
to this record. This is the sole outstanding clean-machine validation rather than a claimed pass.
