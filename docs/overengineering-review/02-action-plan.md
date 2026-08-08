# Action plan

The decisions in this plan are fixed. The first implementation stage deletes isolated complexity;
the credential-check feature is added through existing boundaries; structural scheduler work follows
only after those changes are covered. No step trades away isolation, durability, or data retention for
fewer lines.

## Confirmed scope

| Area | Decision |
|---|---|
| Credential checks | Keep and expose `Provider.check` as an asynchronous diagnostic. |
| Check eligibility | A validly configured provider may be checked whether enabled or disabled. |
| Check concurrency | Reject an active or explicitly pending operation with `409`; do not overlap provider work. |
| Check retries | No automatic retry; the operator explicitly requests another check. |
| Check health effects | Store a separate diagnostic flag; do not mutate normal sync health or scheduling. |
| Check UX | Explicit check action; expose `last_check` on the provider view, separate from sync history. |
| Provider imports | Deprecate Goodreads and Letterboxd imports; retain generic host import support for future providers. |
| Drop-ins | Keep local drop-ins, require `manifest.json` immediately, remove AST fallback now. |
| Scraper support | Retain as deferred contract surface; do not add further abstraction until needed. |
| Provider metadata | Add parity testing first; retain the static manifest and avoid code generation. |

## 1. Low-risk simplifications

These changes should not require a database migration:

1. Deprecate provider-specific imports for Goodreads and Letterboxd, then remove their import
   configuration/export fields, file-import capabilities, provider-specific admission/API affordances,
   and tests. Goodreads RSS appears as complete as its exported CSV; Letterboxd import reads the same
   RSS source as normal fetch. RSS is therefore the sole acquisition path for both providers.
2. Keep the host-owned generic import protocol, queue, and worker path. The fixture provider remains
   the current bundled exercise; future providers may use it when their local export is genuinely
   distinct. Do not deprecate or remove importing in general.
3. Delete the unused legacy schema projection helpers. Restrict API validation/redaction to the
   documented top-level object with scalar, enum, secret, and nullable scalar fields; reject
   unsupported nested objects and combinators.
4. Remove the discarded runner `now` parameter and production `MAX_ARG_STRLEN`; keep payload-size
   assertions local to runner tests. Make lifecycle tests exercise `execute_run()` rather than a
   second test-only runner.
5. Remove the unused HTTP host maps and `_host_lock()` after call-site verification; test actual
   per-host behavior instead of cache implementation details.
6. Remove the legacy provider AST fallback. A drop-in without `manifest.json` is skipped/rejected
   with a warning and is never imported. Update `docs/writing-a-provider.md` and release notes.

Each deletion retains a focused regression test for the behavior that remains. No replacement
framework or generic abstraction is introduced.

## 2. Static metadata parity

Keep `aggregato/providers/manifest.py` as the API-safe, side-effect-free artifact. Add a bundled
provider parity test that compares each loaded provider declaration with its static manifest for:

- id, name, media types, capabilities, acquisition mode, schema version, and poll interval;
- rating scales and their labels/values;
- the supported configuration-schema fields, requiredness, defaults, public/write-only markers, and
  validation-relevant constraints.

Canonicalize only irrelevant schema presentation keys before comparison. A provider declaration
change without a corresponding manifest update must fail with a targeted diff. Do not add a generator,
build dependency, or API-time provider import in this stage.

## 3. Implement the asynchronous credential check

### Public interface

- Add `POST /api/v1/providers/{id}/check`, protected by the existing write authorization and CSRF
  rules. It returns `202` and the existing lineage-shaped response.
- Extend the provider response with `last_check`, containing pending/success/failure status, lineage,
  request/completion timestamps, detail, and error class. A provider with no check has `null`.
- Keep `/providers/{id}/runs` and `/providers/{id}/last-run` about syncs. Check rows are not shown in
  normal sync history; clients observe them through `last_check`.

### Queue and data flow

1. The API validates the provider configuration but does not require the provider to be enabled.
2. It rejects an active run or explicit pending request with `409`; otherwise it writes
   `requested_mode = "check"` and a new lineage into `provider_state`, then makes the provider due.
3. The scheduler claims the request through the existing due-queue boundary. The check gets a
   `sync_runs` row with `mode = "check"`; no new check table or job state machine is added.
4. The child imports only the selected provider, constructs the normal host-owned context, and calls
   `provider.check(ctx)`. The API process never imports or executes provider code.
5. The child emits one tagged check-result message. The parent validates it, records success/failure,
   and closes the run without ingestion, replay, cursor advancement, schema-version changes, retry
   scheduling, or provider-health mutation.
6. `last_check` is derived from the latest check row plus a pending requested check. Reuse the existing
   run diagnostic field for `CheckResult.detail` to avoid a first-pass schema migration.

### Failure behavior

- `CheckResult(ok=False)` records the provider’s `error_class` and detail and does not retry.
- A failed result without an error class, a provider exception, timeout, malformed child message,
  missing result, or unexpected record message is recorded as an internal/failed diagnostic.
- A successful check does not imply a successful sync; it only reports the provider’s own diagnostic.
- A check never changes `ProviderStatus`, retry counters, `last_success_at`, cursor, or
  `next_run_at`.

## 4. Simplify run/job orchestration

After the check path is covered, refactor without changing storage first:

1. Introduce a small typed job/lease record and shared claim, finish, fail, and stale-lease helpers
   for import and replay while retaining both tables.
2. Introduce `RunPlan` and `RunFinalization` records to replace sentinel-heavy `release()` calls and
   repeated `RunRequest` construction.
3. Extract only focused responsibilities from `dispatch.py`: job lifecycle, provider-settings
   projection, and orchestration. Keep one explicit transaction owner.
4. Consider a unified `sync_jobs` table only after behavior is stable. If chosen later, use a
   forward-only migration with previous-revision seeds proving import/replay leases, failures,
   lineage, and payload references survive.

## 5. Retain deferred scraper policy and trim prose

Keep scraper acquisition floors, host coordination, and conformance rules as reserved contract
surface. Do not broaden them until a real provider exercises them.

Then shorten module essays and repeated policy comments:

- module docstrings state purpose, boundary, and one unusual constraint;
- comments explain local security, transaction, or ordering invariants;
- canonical documents hold product behavior and historical rationale;
- frontend architecture documentation says the TypeScript API mirror is hand-curated unless a future
  decision explicitly adds generation.

## Tests and acceptance criteria

Targeted coverage must include:

- valid disabled-provider check, invalid configuration, active-run conflict, and pending-request
  conflict;
- check success, provider-declared failure, timeout, malformed protocol, missing result, and thrown
  exception;
- proof that checks do not ingest, replay, advance cursors, alter retry/schedule/health state, or
  automatically retry;
- child protocol round-trip and exactly-one-result enforcement;
- `last_check` pending/success/failure rendering and exclusion from sync history/latest-sync;
- provider manifest parity and manifest-required drop-ins with no provider import during discovery;
- Goodreads and Letterboxd RSS sync coverage, absence of their provider-specific import affordances,
  and continued generic fixture import coverage;
- unsupported schema rejection and unchanged secret redaction;
- existing import, replay, cursor, supervision, and provider-conformance behavior.

Before merge, run:

```text
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
uv run pytest tests/conformance
uv run pytest
cd frontend && npm run type-check && npm run test:unit
```

## Rollout and compatibility

The check feature is additive and needs no migration in the first implementation. Document the new
check endpoint and provider-view field in OpenAPI and the frontend contract. Deprecating and removing
Goodreads/Letterboxd provider-specific imports is a product-surface change: explain that RSS is now
canonical and that generic importing remains available for future providers. The immediate removal
of legacy drop-in loading is a separate compatibility break: include it in release notes and the
provider-writing guide, with the required manifest format and the “not imported when absent” behavior.

## Explicit non-targets

Do not simplify these merely to reduce line count:

- API/scheduler/child process boundaries;
- parent validation and bounded JSON-lines supervision;
- durable due-queue claims and stale-worker recovery;
- idempotent writes, tombstones, raw payload replay, rating-scale preservation, and keyset cursors;
- authentication, CSRF, secret handling, and image-cache SSRF/content checks;
- SQLAlchemy Core and the two search implementations;
- retained scraper politeness policy;
- independent provider implementations that are short and readable on their own.

These mechanisms are complexity budgets spent to make stated invariants physically true. The purpose
of this review is to reclaim accidental complexity around them, not to weaken the invariants.
