# Prioritized remediation and regression-test plan

This plan is ordered by blast radius and by the number of guarantees each change restores. Do not
start with documentation cleanup while the provider capability boundary and deletion safety remain
broken.

## P0 — Release blockers

| Action | Findings | Done when |
|---|---|---|
| Replace import-all discovery with manifest-only discovery and selected-provider loading | S-01, M-04 | Listing a provider never imports provider code; a child imports only its selected package; an import failure in provider A does not affect provider B. |
| Sanitize the child environment and define the drop-in trust model | S-02, M-08 | A child cannot see API/database/unrelated environment secrets; explicit provider secrets are the only credential input; filesystem/network/resource permissions are documented and tested. |
| Remove provider execution from API requests | S-03, A-01, M-05, M-08 | Replay, check, and import inference are worker/child jobs or the unsupported behavior is removed; API process never calls normalize/check/config inference. |
| Lock down image fetching | S-04, S-07 | SSRF/private-address, redirect, size, content-type, SVG, atomic-write, and concurrency tests pass; only safe cached media is public. |
| Fix full-run sanity baseline ordering | D-01 | A rejected/truncated observation cannot alter the baseline; repeated truncation cannot infer tombstones; the 100 → 1 → 1 regression test passes. |
| Make record writes savepoint-atomic | D-02 | Every rejected record leaves no provider item/work/entry/credit side effect except its failure row; batch processing continues for later records. |
| Add host-failure finalization for runs | R-04 | Any spawn, DB, serialization, or host exception closes the run, records an internal failure, and leaves provider state recoverable. |
| Make misconfigured providers non-runnable | R-01 | Invalid config persists disabled/non-runnable state, is absent from due selection, and manual sync returns a stable conflict. |

These changes should land with regression tests before adding providers or enabling external drop-ins.

## P1 — State machine and data integrity

1. Introduce a durable request/queue record with request ID, lineage, mode, provider, attempt,
   claim owner/lease, and status. Atomically claim it with provider admission. This fixes R-02,
   R-03, R-05, R-06, R-07, R-08, and A-06.

2. Commit run closure, provider status, cursor, retry decision, requested mode consumption, and
   next-run time in one transaction. Preserve disabled state and use an expected generation/version
   in every write.

3. Add import-job leases, failure status, retries, orphan cleanup, quota, and a non-blocking upload
   path. This fixes S-06 and R-07.

4. Persist native IDs in failure envelopes and make replay select every stale schema-version row,
   not only a provider-wide maximum. This fixes D-03 and D-04.

5. Replace no-native-ID pre-select deduplication with a canonical physical unique key and upsert.
   Add concurrent writer tests on both supported databases. This fixes D-05.

6. Centralize external-ID canonicalization and conflict handling. Reject ambiguous work resolution,
   enforce registered namespaces/checksums, and decide whether IDs are globally unique. This fixes
   D-06.

7. Correct merge predicates to compare identifier pairs, prevent parent cycles, add field merge
   policy, and protect undo with optimistic generation checks. This fixes D-07 and D-08.

8. Make search a maintained projection: rebuild affected winner documents after merge, restore,
   replay, tombstone, and multi-provider updates; remove or expose the 500-match cap. This fixes
   D-09 and part of D-11.

9. Namespace or version rating scales and reject incompatible definition changes. This fixes D-10.

10. Set subprocess stream limits explicitly, drain stderr concurrently, and bound total replay/
    output payload sizes. This fixes S-05.

## P2 — Contract, operational, and maintainability hardening

- Replace untyped sync request dictionaries with Pydantic request models (A-02).
- Make redaction schema-aware and strict; add nested/ref/composition tests (A-04).
- Enforce provider_state existence with a transactional upsert and invariant check (A-05).
- Use shared host politeness state, explicit redirect policy, protected request options, and full
  Retry-After parsing (S-08).
- Add authentication throttling, session caps/cleanup, exact public-route matching, and security
  headers (S-07).
- Inject Clock consistently, retaining monotonic time only for pacing/deadlines (R-10, A-11).
- Run migrations under a one-owner or database lock strategy (R-09, A-10).
- Document or paginate fixed API caps and define stats period semantics (A-07, D-11).
- Pin the uv image/base dependencies and publish provenance/SBOM (M-07).
- Repair version/link/contract drift, remove missing plan references, and make the check product
  behavior consistent across code/OpenAPI/README/frontend (M-01 through M-06, M-10, M-11).
- Mark benchmarks correctly and give them a separate budgeted job; add suite timeouts and stuck-test
  diagnostics (M-06, M-09).

## Regression-test matrix

### Security and process boundary

- Importing one broken provider does not prevent another provider from listing/running.
- API requests do not import drop-ins or invoke provider methods.
- Child environment contains no API token, readonly token, database URL, or unrelated sentinel.
- A drop-in cannot write outside its allowed directory or access a forbidden capability, if the
  chosen deployment model promises that restriction.
- Image requests reject private IPs, DNS rebinding, unsafe redirects, oversized bodies, SVG, invalid
  magic bytes, and partial/concurrent writes.
- Child handles max-size lines, stderr saturation, malformed lines, and replay payload limits.

### Scheduler and lifecycle

- Two workers contend for one due provider and exactly one claims it.
- Disable, configure, manual sync, import, and child completion cannot overwrite one another.
- Cursor/release failure injection leaves a recoverable single state.
- API lineage is the lineage visible on the first run and every retry.
- Spawn/DB/serialization exceptions close runs and release/degrade providers.
- Import leases recover after worker death and never run twice.
- Non-poll providers never enter the ordinary due queue.

### Ingestion and identity

- Rejected records leave no partial relational side effects.
- Native ID survives capture/replay even when absent from payload["id"].
- Mixed schema versions replay only stale items.
- Concurrent no-ID writes deduplicate physically.
- Ambiguous external IDs reject rather than silently merging.
- Pairwise merge collisions, cycles, undo-after-new-write, and loser-only titles are safe.
- Search remains correct after every identity/tombstone/replay path.
- Rating scale collision/change is rejected.
- Truncated full runs never poison the baseline or infer deletes.

### Portability and operations

- Run the migration suite, claim races, writer upserts, and search behavior on SQLite and PostgreSQL.
- Start API and worker concurrently against an empty and a previous-revision database.
- CI’s non-bench collection excludes tests/bench, and every suite has a bounded timeout.
- OpenAPI contract tests cover the real check/replay/import asynchronous semantics.

## Suggested implementation sequence

1. Add failing tests for P0 invariants without changing the architecture.
2. Fix provider loading/environment/API execution and image fetching.
3. Fix sanity baseline and per-record transaction atomicity.
4. Introduce durable request/claim state and unify all admission paths.
5. Repair replay identity, external-ID conflicts, merge/undo, and search projections.
6. Add PostgreSQL CI and process-start/migration concurrency tests.
7. Update docs/OpenAPI/frontend, pin build inputs, and remove or finish dead contracts.
8. Re-run all static gates, focused suites, full non-bench pytest, benchmarks separately, and the
   frontend build.

## Exit criteria

The review should be considered closed only when every Critical/High finding has a passing
regression test, the full non-bench suite completes with a final result, PostgreSQL concurrency
coverage exists, and docs no longer promise behavior that is only planned.
