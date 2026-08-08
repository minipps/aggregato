# Overengineering review

**Review date:** 2026-08-08
**Baseline:** `50d48bb` (`v0.2.2`)
**Scope:** Python application, bundled providers, frontend contract surface, tests, and the
architecture documents that constrain them.

## Verdict

Aggregato has a justified complexity core: the API/scheduler process boundary, one child process
per sync, parent-side validation, durable scheduling, idempotent ingestion, and the two database
search implementations all enforce requirements that would otherwise be easy to violate.

Around that core, the code has accumulated several layers that do not currently buy equivalent
product value. The most conspicuous examples are provider-specific behavior in core, two hand-built
metadata sources, a mini JSON Schema interpreter, an unused credential-check contract, and two
nearly identical durable-job state machines. These make the code longer and make future changes
look more general than the product currently is.

The recommended posture is deliberately conservative about abstractions: delete one-off paths,
make the supported configuration subset explicit, and use a few typed records/shared SQL helpers.
Do not replace the current code with a framework, registry hierarchy, or generic workflow engine.

## Highest-value changes

| Priority | Finding | Recommended first move |
|---|---|---|
| P0 | Redundant Goodreads/Letterboxd imports and provider-specific inference | Deprecate both provider-specific import paths; their RSS feeds are the canonical sources. |
| P0 | General-looking schema/redaction machinery | Delete the dead legacy path and reduce validation to the documented flat schema subset. |
| P1 | `Provider.check` has no production caller | Implement it as an asynchronous diagnostic through the existing child/run machinery. |
| P1 | Duplicate import/replay job state machines | Introduce one typed lease/status helper before considering a database-table merge. |
| P1 | `release()` and dispatch lifecycle carry argument/state soup | Add small `RunPlan`/`RunFinalization` records and collapse duplicate orchestration paths. |
| P1 | Hand-maintained provider metadata is duplicated | Add a parity check immediately; retain the static manifest without adding code generation. |
| P1 | Legacy drop-in admission parser | Require manifests immediately and remove the AST fallback. |
| P2 | Future scraper/push/delete scaffolding | Retain it as a deferred contract; add no further abstraction until a real provider exists. |
| P2 | Dead supervision and HTTP state knobs | Delete `MAX_ARG_STRLEN`, the unused `now` parameter, and unused host maps. |
| P2 | Narrative duplication | Keep local invariant comments; move architecture policy to canonical docs and shorten module essays. |

The detailed evidence is in [01-findings.md](./01-findings.md). The staged implementation sequence,
decision points, and acceptance checks are in [02-action-plan.md](./02-action-plan.md).

## Confirmed decisions

The review scope is now fixed:

1. `Provider.check` remains. It becomes an asynchronous diagnostic that uses the existing queued
   child/run path. Validly configured providers may be checked while disabled; an active or explicitly
   pending operation returns a conflict; checks do not auto-retry or mutate normal sync health. The
   result is exposed as a separate `last_check` flag on the provider view rather than mixed into sync
   history.
2. Goodreads and Letterboxd provider-specific imports are deprecated. Their RSS feeds are the
   canonical acquisition paths: Goodreads RSS appears as complete as its exported CSV, and Letterboxd
   import reads the same RSS source as normal fetch. Remove both providers’ import affordances while
   retaining host-owned generic import support for future providers with genuinely distinct exports.
3. Local drop-ins remain supported, but `manifest.json` is required immediately. The legacy AST
   fallback is removed rather than deprecated for one release.
4. Scraper/push/delete-reporting policy remains as reserved contract surface. It is deferred, not
   deleted, and should not receive more abstraction until a concrete provider needs it.
5. Provider metadata keeps a static API-safe manifest for now. A bundled-provider parity test is the
   first simplification; no generator or new build dependency is introduced.

No application code was changed in this pass. This folder now contains the clarified review and an
implementation-ready sequence for a later code change.

## Complexity that should remain

The following are not recommended simplification targets without a changed requirement:

- the API/scheduler split and child-per-run supervision;
- the JSON-lines protocol, message caps, timeout, stderr isolation, and parent validation;
- SQLAlchemy Core, forward-only migrations, keyset pagination, idempotency, tombstones, and raw
  payload replay;
- auth, CSRF, secret resolution/redaction, and image-cache SSRF/content validation;
- the FTS5/Postgres search split;
- the reserved scraper politeness and acquisition policy, which is retained for the future contract;
- straightforward provider duplication where it keeps providers independent and readable.

The test is simple: if removing a mechanism would make a stated invariant physically unenforceable,
keep it. If it exists only for a hypothetical provider, an old compatibility path, or a single
special case, require a product decision before continuing to pay for it.
