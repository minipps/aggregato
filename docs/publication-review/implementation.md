# Publication fixes

Implemented from the [30 September review](README.md), with GPT-6 Luna agents at maximum reasoning
effort and the root agent reviewing the shared code. The original review remains a dated record;
this document describes the implemented changes.

The API, database-backed worker, and supervised provider children remain. The worker now enforces
one owner before recovery and has explicit shutdown handling. A TaskIQ migration is deferred until
distributed execution or an existing broker gives it a concrete purpose.

## Implemented findings

| Review | Change | Evidence |
|---|---|---|
| R1 | Portable export requires an operator. Both public config and the SQLite snapshot omit configured credentials, sessions, jobs, failure payloads, diagnostics, and cursor state. The snapshot is vacuumed so removed strings do not remain in free pages. | [Export and restore regressions](../../tests/integration/test_export_restore.py), [config projection](../../tests/unit/test_config.py) |
| R2 | HTTP diagnostics retain bounded method, sanitized URL, and status only. Legacy response rows are projected on read. Diagnostic and failure-payload endpoints require an operator; viewer summaries hide logs, error details, and cursors. | [Diagnostic access](../../tests/contract/test_run_diagnostics_security.py), [child metadata](../../tests/unit/test_child_response.py), [viewer WebSocket state](../../tests/contract/test_sync_websocket.py) |
| R3 | NULL means unscheduled. Enable and manual sync write due timestamps; a valid config save reactivates an enabled, degraded provider with a suspended schedule. Explicit checks and queued import/replay work remain runnable while disabled. | [Admission and retry transitions](../../tests/unit/test_scheduler.py), [provider endpoints](../../tests/contract/test_providers_endpoints.py), [sync integration](../../tests/integration/test_sync_end_to_end.py) |
| R4 | Replay validates before retiring old entries/opinions, and replacement, review search changes, and item-version advancement share the record savepoint. Rejection preserves the previous valid rows. | [Replay atomicity](../../tests/integration/test_replay_atomicity.py), [writer rollback](../../tests/unit/test_writer_idempotency.py) |
| R5 | Full-run deletion inference is skipped when normalization or ingestion rejects any record, even if the child reports success. | [Full-run failure guards](../../tests/integration/test_replay_atomicity.py) |
| R6 | Manual split credits retain a historical creator UUID and a scoped override during resync. Source and correction-target merges and undo preserve that provenance. Work-row locks serialize a split against ingestion on PostgreSQL. | [Manual durability](../../tests/integration/test_manual_durability.py), [real-format migration survival](../../tests/unit/test_migrations.py), [PostgreSQL writer/split/resync smoke](../../scripts/postgres_smoke.py) |
| R7 | Backend SPA hosting resolves and contains paths within its root; escaping paths and symlinks return 404. | [Static containment](../../tests/integration/test_static_frontend.py) |
| R8 | Nginx forwards WebSocket upgrades with a suitable timeout; Vite proxies sockets in development. Both socket endpoints consume disconnects so idle handlers terminate. | Live proxy and fresh Compose snapshot checks |
| R9 | SQLite file locking and PostgreSQL session advisory locking reject overlapping workers before migrations and recovery. Late completion cannot overwrite an already closed run's scheduling state. | [Worker locks](../../tests/unit/test_worker.py), [stale release](../../tests/unit/test_scheduler.py), live PostgreSQL lock smoke |
| R10 | Shutdown stops admission, drains for three seconds, then cancels and awaits remaining runs and child cleanup. The now-playing monitor stops concurrently; engine disposal happens last. | [Shutdown/reaping regressions](../../tests/unit/test_scheduler.py), warning-as-error container startup and clean shutdown |
| R11 | Settings counts UTF-8 database-rendered JSON bytes on both dialects. Backup capability and SQLite file sizes use the configured URL. Unsupported portable export returns a documented 501. The Docker image includes the existing PostgreSQL driver extra. | [Storage checks](../../tests/unit/test_settings_storage.py), live PostgreSQL SQL and packaged API checks |
| R12 | Replay reads count- and byte-bounded keyset batches. Accepted batches commit before the next child; interrupted runs resume from stale item versions. Oversized and rejected records remain available, and incomplete replay does not fetch. | [Replay batch and interruption checks](../../tests/integration/test_replay_atomicity.py), [writer/replay integrity](../../tests/unit/test_ingest_integrity.py) |

The additional findings are implemented too:

- **C1/C2:** shared request generations ignore obsolete responses; identity mutations expose errors
  and pending state, and undo uses an explicit ID rather than parsing display text.
- **C3/C4/C6:** migration backups happen only before pending upgrades; temporary archive descriptors
  and files are cleaned up; restore validates and stages files before publishing the directory.
- **C5/C7:** retention copy describes resolved failures and retained replay payloads accurately;
  weekly statistics use ISO weeks, including New Year boundaries.
- **C8–C12:** provider forms submit through native validation, emit defaults, and preserve typed
  nullable/enum values. Checks poll boundedly with lineage and stale-response guards. Actions share
  consistent pending state; public users use HTTP fallback and operator controls wait for confirmed
  access. Socket callbacks and fallback responses cannot cross session changes.
- **C13:** date filters explicitly use inclusive UTC bounds through the final microsecond of the day.
- **C14:** runtime version and User-Agent derive from installed package metadata.

## Simplifications and text

- Removed RunPlan and RunFinalization; dispatch uses RunRequest and directly persists its outcome
  through the existing release function.
- Replaced full creator-memo copies with a per-record delta over the shared memo, published only
  after savepoint success.
- Shared the existing flat provider-schema parser and validator between API and config export;
  removed the copied interpretation.
- Moved title/review matching into database subqueries before pagination. The composed query has
  independent binds and uses the correct work ID; migration 0020 adds the entry provider-item index.
- Removed the worker's uvloop setup and selected stdlib asyncio in the container API entrypoint;
  this avoids the locked uvloop version's Python 3.14 deprecations without suppressing warnings.
- Removed unused scheduler boot jitter and empty contract-test exemption scaffolding. Retention
  reuses the existing database upsert.
- Kept native form controls, the small fetch client, static provider discovery, and supported
  backend SPA hosting. No broker, ORM, runtime SDK, or new dependency was introduced.
- Confirmed with a docstring-stripped AST comparison that 37 changed Python files contain only
  prose changes; substantive changes are confined to the reviewed implementation areas.
- Rewrote inaccurate architecture claims, broken requirement references, planning-template prose,
  stale onboarding, and comments that defended code rather than explaining it. README puts
  installation before candidate providers. [Release notes](../../CHANGELOG.md) describe the actual
  changes and acquisition limits.

Migration 0019 stores a historical UUID without a foreign key, since a source creator may later be
merged and deleted. Its previous-revision fixture uses SQLite's real hex columns and hyphenated
JSON snapshots; it covers active source/target merges and stale split history. Neither new
migration rewrites an existing revision.

The maintainer selected AGPLv3. The repository includes the full [license](../../LICENSE),
Python and frontend metadata declare `AGPL-3.0-only`, and the installed Python distribution
includes the license file. [SECURITY.md](../../SECURITY.md) directs private reports to
aggregato@dospuntostr.es.

## Additional validation fixes

Browser validation found that Settings displayed the database image-cache preference even when
instance configuration disabled acquisition. GET/PATCH now expose the effective value and its
configuration gate. The UI locks the checkbox and explains the gate; retention edits preserve the
stored preference. [API regression](../../tests/integration/test_effective_image_cache_setting.py)
and the existing frontend Settings tests cover this behavior. Backup text now explicitly calls
retained history and provider payloads private.

The million-entry API measurement exposed an unnecessary NULL branch in the shared keyset
predicate. Declared non-null columns now use the plain row comparison, allowing SQLite to seek
the timestamp range. Nullable and computed sorts retain their NULL-last behavior. Deterministic
[query-plan](../../tests/bench/test_query_plans.py) and [pagination](../../tests/unit/test_pagination.py)
regressions cover the range bound and cursor semantics. See the [measurements](performance.md)
for before/after timings, writer results, memory, and limits.

The validation guide and remaining frontend comments were cleaned up to remove obsolete commands,
unsupported guarantees, broken references, and explanations that no longer matched the code.

## Verification

The final dependency checks use the existing locks: Python dependencies were synchronized with
`uv sync --frozen --all-extras --dev`, and frontend dependencies with `npm ci` under CI's Node
22. The earlier review's installed-version mismatch is no longer the final verification limit.

| Check | Result |
|---|---|
| Ruff format/check, mypy, import-linter | Passed; 84 application source files, 3 import contracts kept |
| Offline Python suite, including bundled-provider conformance | 822 passed, 15 skipped, 4 benchmark tests deselected |
| Million-entry API and writer measurements | [Local synthetic measurements](performance.md); API first/deep targets met locally, writer scaling target not established |
| Benchmark harness | 4 passed; these sample/plan checks do not prove the declared hardware budgets |
| Focused publication runner | 116 passed |
| Frontend types, unit tests, production build on locked dependencies | Passed; 51 tests in 15 files |
| Backend and frontend Docker builds | Passed using the pinned publication Dockerfiles |
| PostgreSQL 16.14 and 18 smoke | Migrations, claims, release/retry, savepoints, writer idempotency, FTS, singleton lock, Settings SQL, and manual split/resync |
| Packaged PostgreSQL startup | Passed; inert providers, Settings capability/accounting, operator portable-export 501 |
| Fresh SQLite Compose deployment | Passed; SPA/API, safe disabled-provider configuration, operator session, both proxied WebSocket snapshots/disconnects, viewer export refusal, ZIP credential scan, warning-as-error startup, and graceful API/worker shutdown (exit 0) |
| Browser and keyboard checks | [13 axe view checks, keyboard sign-in/filtering, native form validation, and eight responsive layouts](browser-validation.md); five synthetic screenshots |
| Relative documentation links and diff whitespace | Passed; local Markdown link paths and screenshot files checked |

Run the focused regressions from the repository root:

```bash
.venv/bin/python docs/publication-review/reproduce.py
```

The smoke used disposable databases and synthetic credentials. Ordinary pytest still blocks all
sockets; no real provider requests or personal archives were used.

## Remaining publication work and limits

1. Complete the manual browser-zoom, gradient contrast, and screen-reader review. Synthetic
   screenshots, keyboard interactions, and responsive layout checks are recorded in the
   [browser validation report](browser-validation.md).
2. Run the release-commit CI, including its PostgreSQL 16 service and target architectures. Local
   live database checks used PostgreSQL 18 and the exact CI-pinned PostgreSQL 16.14 image; image builds and deployment checks used amd64.
3. Confirm the performance targets on target hardware and a representative archive distribution.
   The local 1M-entry API and writer/memory measurements are recorded; the writer burst comparison
   showed a decline outside the ±10% target. Normal fetch output is still accumulated before ingest.
4. Recheck live upstream provider compatibility before announcing source support. Recorded fixtures
   verify the code contract, not the availability or policy of a service today.

The singleton locks are process/session ownership, not durable distributed leases. A lost
PostgreSQL session releases its lock; multi-host failover still needs explicit ownership design.
Schema changes alone do not reactivate a suspended history sync: update the provider and explicitly
request a sync. The independent now-playing monitor has its own configuration/schema fingerprint.

Portable archives intentionally omit operational evidence but retain personal history and arbitrary
provider-item payloads. They are private backups, not a public sharing format. Existing unsanitized
archives or previously retained diagnostics require separate operator review.

The user's existing Media view, test, and navigation edits are preserved. Changes are grouped by
subject for review; no release tag was pushed.
