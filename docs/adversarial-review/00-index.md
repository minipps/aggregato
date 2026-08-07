# Aggregato adversarial review

Review date: 2026-08-07

This is a source-and-test review of the working tree at commit \`cd89524\` on branch
\`fix/sqlite-scheduler-admission\`. The review is intentionally adversarial: it tests whether the
architecture's stated guarantees remain true under malformed providers, concurrent workers, hostile
URLs, bad data, partial failures, and operational mistakes.

## Executive judgement

The codebase is unusually explicit about its intended boundaries and has good internal hygiene:
Ruff, mypy, import-linter, frontend type-checking, frontend unit tests, runner supervision tests,
and a merge/split integration test passed. The principal concern is not ordinary code quality. It is
that several high-value guarantees described in the architecture are not physically true in the
implementation:

- Provider discovery imports every provider, including unreviewed drop-ins, in the API and child.
- The child inherits the worker's complete environment, including credentials unrelated to the
  selected provider.
- Replay and import configuration inference execute provider code in the API process.
- Scheduler admission, cursor release, requested modes, lineage, and provider status are not one
  atomic state machine.
- A failed full-run sanity observation can become the baseline for the next run and enable inferred
  deletion after repeated truncation.
- A late per-record failure can leave partial relational writes behind.
- The image cache is an unauthenticated, redirect-following server-side fetcher with no response
  size or private-network policy and accepts SVG.

These are release-blocking issues for an installation that treats drop-ins as unreviewed or exposes
the API beyond a trusted loopback. The most urgent data-integrity issue is the full-run baseline
poisoning bug; the most urgent security issue is the provider execution/credential boundary.

## Severity scale

- **Critical**: arbitrary code or secret exposure, or a credible archive-wide data-loss path.
- **High**: likely cross-request/process corruption, denial of service, or incorrect durable data.
- **Medium**: meaningful operational or product failure with narrower blast radius.
- **Low**: maintainability, documentation, defense-in-depth, or future regression risk.

## Review artifacts

- [Security and provider isolation](./01-security-isolation.md)
- [Scheduler and runtime reliability](./02-scheduler-runtime.md)
- [Ingestion, identity, and search integrity](./03-ingestion-identity-search.md)
- [API, configuration, database, and storage](./04-api-config-storage.md)
- [Documentation, CI, dead code, and architecture debt](./05-docs-ci-dead-code.md)
- [Prioritized remediation and regression-test plan](./06-remediation-plan.md)

## Verification status at the review baseline (historical)

The counts and incomplete runs below describe the original adversarial-review snapshot. They are not
current-branch CI evidence; see [the follow-up audit](06-remediation-plan.md#follow-up-audit--2026-08-07)
for the scoped PostgreSQL and documentation status.

Passing checks:

- \`ruff format --check .\`: 144 Python files formatted.
- \`ruff check .\`: clean.
- \`mypy\`: 75 files clean.
- \`lint-imports\`: 123 files, 629 dependencies, 3 contracts, 0 broken.
- Frontend type-check: passed.
- Frontend unit tests: 6 files, 21 tests passed.
- Frontend production build: passed.
- \`tests/unit/test_runner_supervision.py\`: 23 passed.
- \`tests/integration/test_merge_split.py\`: 1 passed.

Incomplete checks:

- The broad pytest run collected 630 tests, ran into the benchmark/conformance portion, then made
  no further progress and was stopped. It did not produce a final result.
- Four individually selected suites (\`test_migrations\`, \`test_providers_endpoints\`,
  \`test_writer_idempotency\`, and \`test_no_inferred_deletes\`) hung on their first test and were
  stopped by a 45-second timeout. This is an environment or test-runtime signal, not a pass.
- \`pytest -m 'not bench'\` still collected all 630 tests. The benchmark files do not carry the
  \`bench\` marker, and the broad run showed benchmark tests executing, so the CI exclusion is not
  effective.

The review did not modify application code. The files in this directory are the review deliverable.

## What is already good

- The dependency direction is machine-checked and currently clean.
- Parent-side validation, JSON-lines protocol validation, checkpoint-aware partial outcomes, and
  child wall-clock termination are clearly designed and have focused tests.
- Authentication uses constant-time credential comparison, HttpOnly/SameSite cookies, CSRF binding,
  and a method-gated read-only token.
- The acquisition policy explicitly rejects CAPTCHA/anti-bot circumvention and has provider
  conformance coverage.
- Migrations are forward-only and the existing SQLite migration tests exercise survival of important
  table-rebuild migrations.
- The default API bind is loopback, CORS is opt-in, and tests block sockets.

Those strengths make the defects actionable: most can be fixed by making the existing stated
contracts transactional or capability-restricted rather than by replacing the architecture.
