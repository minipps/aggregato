# Documentation, CI, dead code, and architecture debt

The documentation is detailed and useful, but it has drifted from the current implementation in
several places. The drift is dangerous here because the docs describe safety guarantees that
operators and contributors will rely on.

This file records the review-time findings. Some items have since been addressed or have new
evidence; use the [follow-up audit](06-remediation-plan.md#follow-up-audit--2026-08-07) rather than
reading an individual finding below as the current repository status.

## M-01 — Version and release examples are stale (Low/Medium)

The project/package and frontend versions are 0.1.5, while README, Docker Compose comments, and
release-related prose refer to 0.2.0. The repository is currently on tag v0.1.5 ancestry.

Fix:

- Generate the displayed version from one source or update all release examples atomically.
- Add a release check that rejects mismatched pyproject, frontend package, Docker comments, and
  documentation versions.

## M-02 — Quickstart documentation points to a missing file (Low)

docs/quickstart-validation.md refers to quickstart.md, but no such root document exists; the
repository's main onboarding document is README.md.

Fix:

- Link to the actual document or restore a deliberately maintained quickstart.
- Add a documentation-link check in CI.

## M-03 — Several documents reference a missing plan.md (Low/Medium)

Architecture/research/provider documents and import-linter comments refer to plan.md and its
complexity tracking, but the file is absent. This makes design provenance and the justification for
some implementation choices impossible to follow.

Fix:

- Replace references with current architecture/research sections, or add and maintain the missing
  decision record.
- Prefer stable document links over historical file names in source comments.

## M-04 — Provider registry documentation contradicts the implementation (Medium)

The registry docstring says drop-in scanning is a later task and deliberately absent, while the
implementation scans and imports drop-ins. See [registry.py:78](../../aggregato/providers/registry.py:78).

This is not merely editorial: the security model, trust assumptions, and API behavior differ
depending on whether drop-ins exist.

Fix:

- Document the actual drop-in lifecycle, trust boundary, import behavior, and capability limits.
- Do not describe import-time conventions as a sandbox.

## M-05 — OpenAPI, README, and frontend disagree with the check route (High contract drift)

The OpenAPI contract says the check verifies credentials without ingesting; README describes reading
one page; the frontend labels the action “Checking credentials”; the route reports the latest sync
and documents that an on-demand check is not built.

Fix:

- Resolve the product decision first, then update implementation, OpenAPI, README, and frontend
  together.
- Add a contract test that exercises the documented behavior rather than only response shape.

## M-06 — CI does not actually exclude benchmarks (Medium)

CI runs pytest with a not-bench expression, but benchmark test files lack the bench marker. The
marker is declared in pyproject and used in documentation, but not applied to the benchmark module
or tests.

Impact:

- Performance budgets run in ordinary CI, making tests slower and contributing to the observed
  non-completing run.
- A benchmark can become a flaky merge gate rather than an explicit performance check.

Fix:

- Add pytestmark = pytest.mark.bench in tests/bench or mark every benchmark test.
- Verify CI collection with a check that no tests under tests/bench appear in the non-bench set.
- Give benchmarks a dedicated job with explicit time/resource budgets.

## M-07 — Container builds use an unpinned external uv image (Medium supply-chain risk)

The Docker build copies uv from ghcr.io/astral-sh/uv:latest. A moving tag makes a historical build
non-reproducible and expands the trusted supply chain independently of the application lockfiles.

Fix:

- Pin the uv image to a version and preferably a digest.
- Pin base image digests or establish a documented update bot/process.
- Generate a software bill of materials and verify provenance in release CI.
- Keep npm/Node and Python dependency lock/update policy explicit.

## M-08 — Dead or half-wired contracts create false confidence (Medium)

At the review baseline, the following paths were present but not connected to the production
behavior their names implied:

- Provider.check exists in the protocol/providers but is not called by the API check route.
- load_config accepts/advertises database overrides, but startup does not load that layer.
- API-generated lineage is returned but not persisted into dispatch/retry state.
- At the review baseline, RunRequest had a secrets field while dispatch supplied an empty mapping and
  the child inherited ambient environment variables instead.
- The replay API path bypasses the worker architecture rather than using the intended child route.

Fix:

- Delete unsupported interfaces, or finish them end-to-end with tests and OpenAPI updates.
- Mark intentionally deferred features as unavailable instead of returning a misleading successful
  shape.
- Add a “contract is exercised in production” checklist for every protocol method and state field.

## M-09 — Quality gates do not cover the highest-risk runtime combinations (Medium)

At the review baseline, static checks and import-linter were good, but the observed pytest hang
meant the primary test gate could fail to produce a result. The reviewed workflow also had no visible
PostgreSQL concurrency/migration gate, despite PostgreSQL being a supported storage backend.

Fix:

- Add per-suite timeouts and process-level test diagnostics so CI reports the stuck test and stack.
- Run deterministic unit/contract tests separately from integration/benchmark jobs.
- Add PostgreSQL migration, claim race, writer atomicity, search, and retry tests.
- Treat a timeout as a failed gate with an actionable artifact, never as an indefinitely running job.

## M-10 — Source comments overstate guarantees (Medium)

At the review baseline, examples included “only this provider is imported,” “provider receives only
its own secrets,” “one in-flight request per host,” and “configuration precedence includes database
overrides.” The corresponding code then violated or only partially implemented each statement.

Fix:

- For every security/architecture comment, add a focused invariant test or weaken the claim.
- Use comments to explain mechanisms and limits, not desired future behavior.
- Make the review artifacts here temporary input to code/documentation fixes, not a second
  permanently divergent specification.

## M-11 — Documentation should expose operational limits (Low/Medium)

The API has fixed history/search caps, upload limits, retained raw payloads, retry/degraded states,
and provider import files, but the public contract does not consistently expose their behavior.

Fix:

- Document caps, retention, recovery, raw-payload privacy, and eventual/asynchronous status in
  OpenAPI and the operator guide.
- Add frontend empty/loading/error/truncated states for each asynchronous operation.
