<!--
SYNC IMPACT REPORT
Version change: TEMPLATE (unversioned) → 1.0.0
Rationale: First ratification. All placeholder tokens replaced with concrete principles.

Principles defined (all new):
- I. Code Quality Is Non-Negotiable
- II. Testing Standards (NON-NEGOTIABLE)
- III. Consistent User Experience
- IV. Decoupling by Default
- V. Plugin System as the Extension Point
- VI. Performance Is a Declared Budget

Added sections: Quality Gates; Development Workflow; Governance
Removed sections: none (template placeholders [SECTION_2_NAME]/[SECTION_3_NAME] resolved)

Templates / docs status:
- ✅ .specify/templates/plan-template.md — Constitution Check filled with concrete gates
- ✅ .specify/templates/tasks-template.md — "tests are OPTIONAL" replaced (conflicted with Principle II)
- ✅ .specify/templates/spec-template.md — reviewed, no change needed (Success Criteria already
     carries measurable outcomes; Principle VI budgets live in plan.md Technical Context)
- ✅ .specify/templates/checklist-template.md — reviewed, generic, no change needed
- ⚠ README.md / docs/ — do not exist yet; restate principles there when authored

Deferred / TODO: none
-->

# Aggregato Constitution

## Core Principles

### I. Code Quality Is Non-Negotiable

Code MUST be readable before it is clever: a reviewer unfamiliar with the change understands it
without asking the author. Every merged change MUST pass the project's formatter, linter, and type
checks with zero warnings — warnings are errors, not backlog. Public functions, types, and plugin
contracts MUST carry doc comments stating purpose, inputs, and failure modes; private helpers need
none. Dead code, commented-out blocks, and speculative abstractions MUST NOT be merged: an
interface with a single implementation, a factory for one product, or configuration for a value
that never changes are all rejected until a second real case exists. Duplication is preferred over
the wrong abstraction; the third occurrence justifies extraction.

*Rationale*: This project's cost centre is comprehension, not typing. Every warning tolerated and
every unused abstraction merged is paid for again by whoever is debugging at 3am.

### II. Testing Standards (NON-NEGOTIABLE)

Every behavioural change MUST land with an automated test that fails before the change and passes
after. Bug fixes MUST include a regression test reproducing the reported symptom. Tests MUST be
deterministic: no wall-clock sleeps, no network access, no reliance on execution order; time,
randomness, and I/O MUST be injectable. Test coverage requirements by layer:

- **Unit**: every branch of non-trivial logic (parsers, schedulers, deduplication, money/security
  paths).
- **Contract**: every plugin contract MUST have a shared conformance suite that any plugin — first
  or third party — can run to prove compliance.
- **Integration**: every user-facing journey named in a spec MUST be exercised end-to-end at least
  once, using real plugin implementations rather than mocks of our own code.

Coverage percentage is a diagnostic, never a target. An untested `if` is a defect regardless of
the number. Trivial one-liners and pure re-exports need no test.

*Rationale*: An aggregator's failure modes are dull and expensive — a source silently returning
nothing, a duplicate slipping through. Only tests that pin behaviour catch these before users do.

### III. Consistent User Experience

The system presents one coherent surface, not the union of its plugins. Therefore:

- Aggregated items MUST be normalised into the core's canonical shape before reaching any UI or
  API consumer; plugin-specific vocabulary MUST NOT leak outward.
- Error presentation MUST be uniform: a failing source degrades that source only, is reported in
  the same shape as every other failure, and never blanks the whole view.
- Naming, ordering, date/time rendering, pagination, and empty/loading/error states MUST follow
  one documented set of conventions across every surface (CLI, API, UI). A new surface reuses
  those conventions or amends them for all surfaces — never forks them locally.
- Accessibility basics are mandatory on every user-facing surface: keyboard reachability, semantic
  structure, and text alternatives. These MUST NOT be deferred as polish.

*Rationale*: Users judge an aggregator by whether heterogeneous sources feel like one product.
Inconsistency reads as breakage even when every part works.

### IV. Decoupling by Default

Dependencies MUST point inward toward the core domain, never outward. Specifically:

- The core MUST NOT import, name, or special-case any individual source, storage engine, or
  presentation framework. If the core needs to know which plugin it is talking to, the contract is
  wrong.
- Modules MUST communicate through explicit contracts (types, interfaces, events) — never through
  shared mutable global state or reaching into another module's internals.
- Any module SHOULD be replaceable by a fake in tests without touching the code under test. A
  module that cannot be faked is too coupled and MUST be refactored before new features are built
  on it.
- Cyclic dependencies between modules are forbidden.

*Rationale*: Decoupling is what makes the plugin system possible and the test suite fast. Both
collapse the moment the core knows a source's name.

### V. Plugin System as the Extension Point

Sources, transforms, and outputs are plugins. New capability of these kinds MUST be added as a
plugin, not as a branch in core code. The plugin system MUST satisfy:

- A **versioned, documented contract**: what the host guarantees a plugin, what a plugin must
  provide, and how incompatibility is detected and reported. Contract changes follow semantic
  versioning; breaking changes require a MAJOR bump and a documented migration path.
- **Isolation of failure**: a plugin that throws, hangs, or returns malformed data MUST be
  contained — the host applies timeouts, validates plugin output at the boundary, and continues
  serving every other plugin.
- **No privileged plugins**: bundled first-party plugins MUST use exactly the same contract and
  registration path as third-party ones. Any capability a first-party plugin needs is added to the
  public contract or not used.
- **Discoverability**: plugins declare their identity, version, and required host version
  declaratively; the host MUST NOT require code changes to load a conforming plugin.

*Rationale*: A plugin system only earns its complexity if it is the sole path for extension. The
day core code special-cases one source, the contract stops being trustworthy and every future
plugin author pays.

### VI. Performance Is a Declared Budget

Every feature MUST declare its performance budget in `plan.md` before implementation — the
relevant latency, throughput, and memory numbers for that feature's workload. Rules:

- Budgets MUST be measured, not asserted. A feature with a budget and no measurement is
  incomplete.
- Regression rule: a merged change MUST NOT worsen a recorded baseline by more than 10% on any
  declared metric without an explicit, documented justification recorded in the plan's Complexity
  Tracking table.
- Aggregation work MUST be concurrent across sources and MUST NOT block on the slowest source
  beyond its declared timeout; one slow source degrades that source only.
- Optimisation MUST follow measurement. Profile-free optimisation is rejected in review; a naive
  implementation with a known ceiling is acceptable when the ceiling and upgrade path are
  documented in a code comment.

*Rationale*: "Fast" is unenforceable; a number in the plan is. Declared budgets also make it
obvious when a plugin, not the core, is the bottleneck.

## Quality Gates

A change MUST NOT merge unless all of the following hold:

1. Formatter, linter, and type checks pass with zero warnings (Principle I).
2. The full test suite passes; new behaviour has a test that failed before the change
   (Principle II).
3. Plugin contract conformance suite passes for every bundled plugin (Principles II, V).
4. No new dependency from core onto a concrete plugin, storage engine, or UI framework
   (Principle IV).
5. New user-facing surface follows the documented UX conventions and accessibility basics
   (Principle III).
6. Declared performance budgets are measured, with no undocumented regression beyond 10%
   (Principle VI).
7. Any new dependency is justified in the PR description against the alternative of stdlib,
   native platform features, or already-installed packages (Principle I).

## Development Workflow

- Work proceeds spec → plan → tasks → implement. The plan's Constitution Check gate MUST be
  evaluated before Phase 0 research and re-evaluated after Phase 1 design.
- Every principle violation that survives design MUST be recorded in the plan's Complexity
  Tracking table with the simpler alternative and why it was rejected. An unrecorded violation is
  a review blocker.
- Reviews assess constitutional compliance first and style preference last. A reviewer citing a
  principle number blocks; a reviewer citing taste suggests.
- Contract changes to the plugin system MUST be reviewed as public API: version bump, migration
  note, and conformance suite update in the same change.

## Governance

This constitution supersedes all other development practices, conventions, and habits in this
repository. Where a tool's default conflicts with a principle here, the principle wins and the
tool is reconfigured.

**Amendment procedure**: Amendments MUST be proposed as a change to this file that states the
motivating problem, the new or revised text, and the migration impact on existing code and
templates. An amendment takes effect when merged; the Sync Impact Report at the top of this file
MUST be updated in the same change.

**Versioning policy**: This constitution is versioned semantically.

- **MAJOR**: a principle is removed or redefined in a backward-incompatible way.
- **MINOR**: a principle or section is added, or existing guidance is materially expanded.
- **PATCH**: clarification, wording, or typo fixes that do not change what is required.

**Compliance review**: Every pull request verifies the Quality Gates above. Dependent templates in
`.specify/templates/` MUST be checked for drift whenever this file changes, and any pending
template update MUST be listed as ⚠ in the Sync Impact Report until resolved.

**Version**: 1.0.0 | **Ratified**: 2026-07-29 | **Last Amended**: 2026-07-29
