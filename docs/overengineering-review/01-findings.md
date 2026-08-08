# Findings

## Review method

This is an adversarial maintainability review, not a second security audit. I inspected the current
implementation at `50d48bb`, searched call sites for public-looking helpers and contract methods,
compared the implementation with the canonical architecture/provider documents, and checked where
complexity is duplicated or exists without a current consumer. Existing
`docs/adversarial-review/` material was treated as historical context; fixed security and reliability
issues were not re-filed here.

The severity labels describe simplification value, not exploitability:

- **P0:** delete or decide before adding more behavior;
- **P1:** meaningful structural simplification with bounded migration work;
- **P2:** low-risk cleanup or deferred product surface.

Line references point to the review baseline and are intended as starting points for implementation.

## OE-01 — Goodreads/Letterboxd imports duplicate RSS and leak provider knowledge into core

**Severity:** P0
**Evidence:** `aggregato/api/routes/providers.py:675` branches on
`info.import_inference == "letterboxd_rss_username"`; the parser at `:685` knows the
`letterboxd.com` URL shape. The field is carried through `aggregato/providers/registry.py:84` and
the only manifest value is in `aggregato/providers/manifest.py:295`.

The architecture says core knows no provider’s name. These paths break that rule for conveniences
that are only implemented for two providers. Letterboxd import reads the same RSS source as the
normal Letterboxd fetch, so it does not provide a distinct acquisition surface. Goodreads’ RSS
appears to contain the same useful history as the exported CSV it offers, so its CSV import also does
not currently provide a distinct product surface. Adding more provider-specific import inference
would require another core string, branch, parser, and manifest convention. It is a classic
“generic-looking” extension point whose implementations are actually one-offs.

**Recommendation:** deprecate provider-specific imports for both Goodreads and Letterboxd, then remove
their import configuration/export fields, `Capability.FILE_IMPORT` declarations, provider-specific
import admission/API affordances, and tests. Remove `import_inference`,
`_settings_inferred_from_import`, `_infer_letterboxd_username`, and the Letterboxd provider-owned
import helper. The ordinary RSS fetch is the sole acquisition path for both providers. Keep host-owned
generic import machinery for future providers whose local export is genuinely distinct; the fixture
provider remains the current bundled exercise of that generic path. Do not grow a core switchboard
for another redundant source.

**Acceptance check:** Goodreads and Letterboxd have no import controls or import capabilities in their
manifest/API views; both normal RSS syncs still work; generic import remains available for the fixture
provider and future providers; registry discovery remains side-effect free and the API has no
provider-specific import branch.

## OE-02 — The API contains a mini JSON Schema implementation plus a dead predecessor

**Severity:** P0
**Evidence:** `aggregato/api/routes/providers.py:786` starts the unused legacy redaction path
(`_legacy_public_settings`, `_public_value`, and related helpers). The active path from `:834` to
`:1045` separately projects public settings and interprets `$ref`, `anyOf`, `oneOf`, `allOf`, arrays,
and JSON types.

The provider contract documents configuration as flat scalar values, enums, and secrets
(`aggregato/providers/base.py:140`). The bundled manifests use that small subset, and
`frontend/src/components/SchemaForm.vue` renders the same small subset. The implementation pays
for a general schema vocabulary that neither the product nor the UI supports, while retaining a
second implementation that repository search finds no caller for.

This is risky as well as verbose: a partial schema interpreter creates edge cases that look like
JSON Schema compliance but are not. Each supported keyword also has to stay aligned between API,
manifest, and frontend.

**Recommendation:**

1. Delete the legacy projection helpers.
2. Define the supported manifest schema explicitly: top-level object; string, number, boolean,
   enum, and nullable scalar fields; secret/write-only metadata.
3. Replace the generic traversal with one small validator/redactor for that subset. Reject an
   unsupported schema at discovery/validation time instead of partially interpreting it.

Do not introduce another validation framework merely to preserve an unused generality.

**Acceptance check:** unsupported nested objects/arrays and unsupported combinators fail clearly;
secrets never appear in API responses; all current provider forms and invalid-settings tests still
pass.

## OE-03 — Provider metadata has two hand-maintained sources of truth

**Severity:** P1
**Evidence:** a provider declares its identity, capabilities, acquisition mode, configuration,
rating scales, interval, and schema version in its module (for example
`aggregato/providers/anilist/__init__.py:104`). The API-facing copies are repeated in
`aggregato/providers/manifest.py:188` onward and are converted again by
`aggregato/providers/registry.py:273`.

The parent/API deliberately consumes static metadata so discovery does not import or execute an
unreviewed provider. That boundary is justified. Hand-editing the same facts twice is not: a schema
version, capability, or required setting can drift between what the API displays and what the child
actually runs. The current tests exercise both surfaces but do not enforce parity between them.

**Recommendation:** add a parity test now so drift becomes an immediate failure, but keep the static
manifest hand-maintained for the moment. Compare the provider declaration with the static artifact,
including the supported configuration-schema fields, and fail with a targeted diff. This repository
has no existing code-generation step; adding one would trade one form of duplication for a new build
tool and release concern. Revisit generation only if the parity test demonstrates recurring drift.

Keep the static artifact for safe API discovery; do not solve this by importing arbitrary drop-ins in
the API process.

**Acceptance check:** changing a bundled provider’s declaration without regenerating its manifest
fails CI with a targeted diff; API discovery remains inert and drop-in isolation is unchanged.

## OE-04 — `Provider.check` is an implemented contract with no runtime entry point

**Severity:** P1
**Evidence:** `aggregato/providers/base.py:217` requires `check` and `CheckResult`; all six bundled
providers implement it, and conformance group 7 in `tests/conformance/test_providers.py:335` tests
it. Repository search finds no API, scheduler, or dispatch caller. The provider route explicitly
distinguishes the latest run from a credential check at `aggregato/api/routes/providers.py:383`.

The result is six implementations and conformance fixtures for a feature users cannot currently
invoke. The contract is still valuable as an explicit operator diagnostic, but it must be wired into
the same isolation and durability model as syncs rather than left as a test-only surface.

**Recommendation:** implement an asynchronous check without creating a separate worker architecture:

- Add `POST /providers/{id}/check`, returning the existing lineage-shaped `202` response.
- Queue it through `provider_state.requested_mode = "check"`; use `sync_runs.mode = "check"` for
  durable history and reuse the existing child process, runner, and scheduler boundary.
- Add a tagged check-result message to the child protocol. A check produces no normalized records,
  replay work, cursor changes, ingestion, schema-version updates, or sync retry.
- Allow a valid configuration to be checked while its provider is disabled. Return a conflict when a
  provider already has an active run or explicit pending request. Do not automatically retry a check.
- Treat the result as diagnostic-only: do not change provider status, retry counters, last-sync
  timestamps, cursor, or next scheduled run. Expose pending/success/failure, detail, and error class
  as `ProviderView.last_check`; keep it out of normal sync history and the latest-sync endpoint.
- Reuse the existing run diagnostic field for `CheckResult.detail` in check rows rather than adding a
  new table or column in the first implementation.

Provider exceptions, timeouts, malformed protocol, missing results, and invalid failed results become
failed diagnostics with an internal/error classification. The API must never run `check` directly.

## OE-05 — Import and replay jobs duplicate the same durable state machine

**Severity:** P1
**Evidence:** `aggregato/db/schema.py:487` and `:557` define separate tables with the same lease,
finish, failure, and error concepts. `aggregato/sync/dispatch.py:170` onward repeats next/finish/fail
helpers for import and replay. `aggregato/sync/scheduler.py:133` repeats due predicates for both
families, and `aggregato/api/routes/runs.py:127` adds another replay lifecycle view.

Durability and crash recovery are justified. The duplication is not: every lifecycle change must be
made twice, and the dispatcher carries branches for job priority, lineage, and state transitions that
could be represented as data.

**Recommendation:** first introduce a small typed job record and shared lease/status SQL helpers
while retaining the two tables. Once behavior is proven, consider a forward migration to one
`sync_jobs` table with a `kind` discriminator and a typed payload reference. Do not replace this with
an in-memory queue or hide the state machine behind a generic task framework.

**Acceptance check:** import and replay preserve their current priority, retry, lineage, stale-lease
recovery, and failure semantics; migration tests seed both old tables and verify rows survive.

## OE-06 — Dispatch and `release()` use parameter/state soup

**Severity:** P1
**Evidence:** `aggregato/sync/scheduler.py:335` exposes `release()` with roughly two dozen optional
keyword arguments and an `_UNSET` sentinel. `aggregato/sync/dispatch.py:464` gives `run_once()` a wide
set of independent inputs, while `_run_opened()` at `:571` reconstructs similar state. The same
module also owns job claims, schema replay, config redaction, child execution, ingestion, sanity
checks, run closure, retry scheduling, and provider-state updates; it is 1,084 lines at review time.

This is not just a large file. It makes invalid combinations representable: a caller can omit a run
field, pass a stale lineage, or supply a state combination that only another branch understands. The
sentinel-heavy release signature is a symptom that one operation is closing several concepts at once.

**Recommendation:** add two or three small immutable records, not a class hierarchy:

- `RunPlan` for the selected mode, lineage, replay/import inputs, and provider settings;
- `RunFinalization` for outcome, cursor/checkpoint, provider state, retry decision, and run closure;
- optionally a typed job lease shared with OE-05.

Make one orchestration path consume those records and keep the transaction boundary explicit. Extract
job lifecycle and configuration projection into focused modules. Avoid a “workflow engine” or strategy
objects for each mode.

**Acceptance check:** call sites no longer pass sentinel-filled keyword lists; ordinary, replay, and
import runs share the same close/reschedule path; transaction ordering and cursor rules remain tested.

## OE-07 — Cross-cutting support exists for providers that do not exist yet

**Severity:** P2 accepted/deferred
**Evidence:** `Capability.SCRAPES` and `Capability.PUSH` are marked reserved in
`aggregato/domain/enums.py:124`; `REPORTS_DELETES` has no current provider. No bundled provider
currently declares scraping, push, or delete reporting. Nevertheless `aggregato/providers/http.py`
contains acquisition-specific floors, per-host process state, file-lock implementations, and
concurrency policy for that future surface.

Scraping rules are legitimate requirements if a scraper is shipped. The current repository has no
consumer, so every refactor must preserve behavior that cannot be exercised by the bundled suite.
This is future-proofing tax, not a present product capability.

**Recommendation:** retain the current acquisition floors, host-state coordination, scraper
conformance rules, and reserved capability values as deferred contract surface. Do not delete or
expand them in this simplification pass. Revisit the cost when the first concrete scraper, push
provider, or delete-reporting provider is accepted.

## OE-08 — Runner exposes dead knobs and tests a second runner

**Severity:** P2
**Evidence:** `aggregato/sync/runner.py:49` defines `MAX_ARG_STRLEN`, but production spawning uses
stdin and does not consult it; only `tests/unit/test_runner_supervision.py:372` imports it to size a
test payload. `execute_run()` accepts `now` at `runner.py:136` and immediately discards it. The same
test file’s `_run_child()` helper at `:112` repeats production timeout/status/protocol
classification instead of exercising `execute_run()` for lifecycle behavior.

The production boundary is valuable, but these knobs obscure which behavior is real and which exists
only to make a test harness convenient.

**Recommendation:** remove the unused `now` parameter and production `MAX_ARG_STRLEN`; use a local
test constant or a literal payload-size assertion. Consolidate lifecycle tests around `execute_run()`
and keep lower-level tests focused on protocol parsing and message classification.

## OE-09 — Source comments repeat the architecture manual

**Severity:** P2
**Evidence:** long module essays appear in `aggregato/sync/dispatch.py:1`,
`aggregato/sync/scheduler.py:1`, `aggregato/sync/runner.py:1`, `aggregato/sync/child.py:1`,
`aggregato/providers/base.py:1`, `aggregato/providers/http.py:1`, and `aggregato/config.py:1`.
Many functions then repeat the same policy rationale in prose. The same material is already
normative in `docs/architecture.md`, `docs/research.md`, `docs/contracts/provider-plugin.md`, and
the contributor guidance.

This makes a small function read like a design document and creates multiple places that can go
stale. It also encourages comments that describe an external requirement without explaining the
local invariant being protected.

**Recommendation:** trim module docstrings to purpose and process boundary. Keep comments that
explain local transaction ordering, security checks, or non-obvious compatibility behavior, ideally
in three lines or fewer. Put normative policy and historical rationale in the canonical document;
do not repeat document identifiers in every function.

This is a readability pass, not a request to remove safety explanations.

## OE-10 — HTTP host maps are dead compatibility state

**Severity:** P2
**Evidence:** `aggregato/providers/http.py:340` initializes `_host_locks` and `_host_limiters`, and
`:382` populates them. Production code uses the consolidated `_HOST_STATES`/`HostState` path instead;
repository search finds no production consumer of either map or of `_host_lock()` at `:365`. The
only observed consumer is `tests/unit/test_http_politeness.py:202`, which asserts map length.

The test currently protects an implementation detail rather than a behavior. Keeping two caches makes
future changes look like they must update both.

**Recommendation:** delete the unused maps and `_host_lock()` if call-site search remains clean;
change the test to assert the actual per-host behavior (or remove the assertion). Preserve the single
host-state map, rate limiting, retry handling, and scraper policy.

## OE-11 — The OpenAPI/TypeScript mirror is intentionally manual but documented as generated

**Severity:** P2
**Evidence:** `frontend/src/api/types.ts:1` calls itself a hand-written mirror, while
`docs/architecture.md` describes a generated-from-OpenAPI client. `frontend/src/api/client.ts:1`
manually enumerates endpoint functions and `docs/contracts/openapi.yaml` is maintained separately.

Manual types are not automatically overengineering; adding a code generator would add a dependency,
build step, and generated diff noise. The actual problem is the mismatch between the promised source
of truth and the maintenance model.

**Recommendation:** first correct the architecture documentation and state that the frontend types
are a deliberately small, hand-curated response subset. Revisit generation only if endpoint churn or
contract drift produces repeated defects; do not add code generation speculatively.

## OE-12 — Legacy drop-in admission doubles the provider-loading path

**Severity:** P1
**Evidence:** `aggregato/providers/registry.py:159` supports manifest-backed drop-ins, while
`:341` retains an AST fallback parser for legacy drop-ins without a manifest. The practical provider
guide documents both paths.

The isolated drop-in boundary may be useful, but the legacy fallback means the registry owns a second
parser and compatibility contract for unreviewed code. It also makes it harder to say exactly which
metadata is required before a provider can appear in the UI.

**Recommendation:** make `manifest.json` mandatory immediately and remove the AST fallback parser.
An unreviewed package without a manifest is skipped/rejected with a clear diagnostic without importing
its code. Update the provider guide and release notes to make this an intentional compatibility break.
Keep the no-import, side-effect-free discovery rule and the `unreviewed` label.
