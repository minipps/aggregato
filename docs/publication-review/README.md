# Publication review

**Implementation update:** the findings below describe the original reviewed snapshot. See
[Publication fixes](implementation.md) for the implemented changes, current verification, and
remaining publication work. Finding tables and source line numbers below are historical evidence,
not a list of defects still open in the working tree.

Reviewed **30 September 2026**, against commit **`d93cf08`**, on `main`, including the existing
uncommitted Media view and navigation changes. The initial review changed documentation only.

**Release recommendation: hold publication until the credential leaks, unsafe replay/deletion,
and never-retry scheduling defect are fixed.** The code has useful boundaries and substantial test
coverage, but several advertised guarantees fail in the current implementation.

**The scheduler does need work. Replacing it with TaskIQ is not a prerequisite for publication.**
The problem is overlapping lifecycle state and insufficient ownership checks. TaskIQ can replace
task delivery and scheduling infrastructure; it cannot replace Aggregato's ingestion, checkpoint,
identity, and archive-safety rules. A migration before fixing those rules would carry defects into
a larger deployment. The [scheduler assessment](#scheduler-and-taskiq) explains the tradeoff.

## How to read this review

- **Critical:** credential exposure to a less privileged reader.
- **High:** archive integrity, repeated prohibited acquisition, or a broken deployment feature.
- **Medium:** operational, portability, usability, or scalability defects with a narrower trigger.
- **Low:** readability, unnecessary complexity, or publication polish.
- **Reproduced:** demonstrated with disposable data before implementation; the original assertions
  have since been replaced by regression tests run through [reproduce.py](reproduce.py).
- **Traced:** follows directly from the inspected code; no end-to-end reproduction was run.
- **Risk:** a concrete scaling or concurrency concern that still needs measurement or a race test.

Line links refer to this snapshot. Earlier [security](../adversarial-review/00-index.md) and
[complexity](../overengineering-review/00-summary.md) audits were background material, not current
proof. Their closed findings were not automatically reopened. In particular, discovery is now
manifest-only, child environments are restricted, ingestion has per-record savepoints, and the
old general schema traversal has been removed.

## Verification and limits

| Check | Result |
|---|---|
| Ruff formatting | 167 existing Python files clean before adding review artifacts |
| Ruff lint | Clean |
| mypy | 83 application source files clean |
| Import boundaries | 3 contracts kept; 139 files and 775 dependencies analyzed |
| Python non-benchmark suite | **771 passed, 15 skipped, 6 deselected**, 51.42 seconds; includes bundled-provider conformance |
| Benchmark suite | **6 passed**, 1.12 seconds; limitations below |
| Frontend type checking | Passed |
| Frontend unit tests | **27 passed in 10 files** |
| Frontend production build | Passed |
| Isolated defect reproductions | 11 confirmations across 8 checks; see command below |
| Review artifacts | Ruff format/check clean across 168 Python files; reproduction script also passes mypy |

These checks used the installed `.venv` and `node_modules`, not a newly synchronized environment.
`uv run --offline` could not synchronize because the locked mypy wheel was absent from the cache.
The installed tools differ from the locks: Ruff 0.16.0 vs 0.16.9, mypy 2.3.0 vs 2.3.1,
Vite 8.2.1 vs 8.3.0, Vitest 4.1.10 vs 5.0.1, Vue 3.5.41 vs 3.5.43, and vue-router 5.2.0 vs 5.3.1.
The passes are useful evidence, not a substitute for CI on a clean locked install.

No production archive or credentials were accessed. No real provider requests were made.
PostgreSQL execution, Docker image builds, a deployed browser session, live provider compatibility,
screen-reader behavior, and ARM performance were not verified here. CodeGraph had no callable tool
and its local directory contained only `.gitignore`; navigation used source search instead.

Run the focused regression checks from the repository root:

```sh
PYTHONPATH=. .venv/bin/python docs/publication-review/reproduce.py
```

The runner invokes the existing offline regression tests for the repaired findings. It reuses
pytest's socket-blocking fixtures and temporary databases instead of maintaining a second set of
reproduction helpers. It is a focused check, not a replacement for the full release gates.

## Release findings

| Finding | Severity | Evidence | Publication action |
|---|---|---|---|
| R1. Export exposes provider credentials and sessions | Critical | Reproduced | Fix before publishing |
| R2. HTTP diagnostics retain OAuth tokens | Critical | Reproduced capture; traced API access | Fix before publishing |
| R3. Never-retry failures are immediately scheduled again | High | Reproduced | Fix before publishing |
| R4. Failed replay hides previously valid archive data | High | Reproduced | Fix before publishing |
| R5. Full-run delete inference treats failed records as absent | High | Reproduced guard | Fix before enabling delete inference |
| R6. Manual creator splits are undone by resync | High | Reproduced | Fix before advertising durable corrections |
| R7. Backend SPA fallback permits path traversal | High, conditional | Reproduced | Fix or remove the fallback before publishing |
| R8. Production proxy does not support WebSocket upgrades | High | Traced | Fix and verify through Compose |
| R9. Startup recovery can steal another worker's active run | High, conditional | Reproduced state transition | Enforce one worker or add ownership |
| R10. Shutdown does not await the sync drain | Medium | Traced | Fix before unattended deployment |
| R11. PostgreSQL settings and export are incomplete | Medium | Traced | Correct implementation or narrow support claims |
| R12. Large normalization replay cannot make progress | High at scale | Traced | Chunk replay before promising large archives |

### R1. Export exposes provider credentials and active session rows

Locations: [archive creation](../../aggregato/export.py#L21),
[export route](../../aggregato/api/routes/export.py#L18),
[provider config persistence](../../aggregato/api/routes/providers.py#L237),
[read access gate](../../aggregato/api/deps.py#L194).

`build_archive()` copies the complete SQLite database. Only the separate `configuration.json`
is passed through `public_dict()`. UI-supplied tokens, client secrets, and refresh tokens remain in
`providers.config`; session rows remain in `sessions`. The reproduction saves a dummy provider token,
issues an operator session, and downloads the ZIP as both a read-only bearer and an unauthenticated
public reader. Both receive the token and the session row.

This violates the Settings promise that secrets and active sessions are excluded. The existing
[export test](../../tests/integration/test_export_restore.py#L88) checks only `configuration.json`.
The copied session rows do not alone provide a valid signed operator cookie; the confirmed
credential exposure is the provider configuration.

**Fix:** require operator privileges for backup/export independently of HTTP method. Sanitize the
snapshot, including provider settings, sessions, and credential-bearing diagnostics. Remove secret
values from literal YAML settings too: `Config.public_dict()` replaces environment references but
does not identify arbitrary literal provider credentials. Audit every archive member, not just the
configuration member. If credentials were already shared through exported archives, rotate them.

**Acceptance:** private backup succeeds for an operator; read-only/public export is refused unless a
separate deliberately public format exists; all members are inspected for dummy secret values and
session rows. A restore remains browsable and requires fresh provider credentials.

### R2. OAuth responses are recorded as raw diagnostics

Locations: [response capture](../../aggregato/sync/child.py#L48),
[HTTP callback](../../aggregato/providers/http.py#L481),
[Spotify token request](../../aggregato/providers/spotify/__init__.py#L282),
[diagnostic API](../../aggregato/api/routes/runs.py#L207).

The capture strips query strings and two response headers but copies the response body unchanged.
The same callback handles Spotify's token exchange, so its `access_token`, and any returned
`refresh_token`, enter `RunOutcome.raw_responses` and the run table. Diagnostic GETs use the ordinary
read gate, making them available to read-only credentials and public readers when public mode is on.
The capture reproduction uses dummy token values; no Spotify request is needed.

**Fix:** make response bodies opt-in, exclude authentication exchanges, and restrict operational
diagnostics and retained failure payloads to operators. If body capture remains, apply a defined
redaction policy before persistence. Deleting a key named `token` alone does not cover raw text,
headers, error messages, or private platform payloads. Review previously retained diagnostics.

**Acceptance:** a mocked token exchange cannot put credentials into stdout, run rows, diagnostic
responses, or exports. Public/read-only archive browsing does not expose raw operational payloads.

### R3. A NULL schedule means both “first run” and “never retry”

Locations: [due predicate](../../aggregato/sync/scheduler.py#L125),
[admission](../../aggregato/sync/scheduler.py#L201),
[failure policy](../../aggregato/sync/retry.py#L148).

The retry policy returns `next_run_at=None` and `status=degraded` for `auth`, `blocked`, and
`structure_changed`. The due predicate explicitly treats NULL as due, while admission permits
enabled degraded providers. All three classifications are immediately due and claimable in the
reproduction. The normal five-second loop can repeatedly revisit a platform the UI says is paused.

**Fix:** give NULL one meaning. A simple option is “unscheduled”: enable/manual requests write an
explicit due time, and ordinary due selection requires a timestamp. Preserve the intentional
exceptions for queued imports, replay, and credential checks. Update both listing and atomic claim.

**Acceptance:** exercise failure policy → release → next scheduler poll for all three classes;
no acquisition resumes without an explicit operator action. Also test first enable and manual sync.
Testing the retry decision in isolation misses this integration defect.

### R4. Replay retires old facts before replacement is safely written

Locations: [replay execution](../../aggregato/sync/dispatch.py#L554),
[tombstoning](../../aggregato/ingest/normalize_replay.py#L33),
[per-record normalization failure](../../aggregato/sync/child.py#L180).

A child can complete successfully while emitting `FailureMessage` for individual records.
Dispatch checks only the run status, then tombstones derivatives for **all input native IDs** in a
separate committed transaction. A failed record has no replacement batch. The reproduction retains
a valid entry, replays its payload with a normalization failure, and observes a tombstone together
with a successful run result. Parent-side rejection or a later ingestion exception creates the
same unsafe transaction ordering.

**Fix:** retire and replace a successfully validated record's derivatives in the same savepoint.
Leave old facts and old item schema versions intact for rejected records. Report incomplete replay
and keep it eligible for retry. Successful replay must mean durable replacement, not child exit 0.

**Acceptance:** normalization failure, writer rejection, and a failure between retirement and write
all preserve the last valid entry/opinion. A successful changed mapping removes only obsolete facts.

### R5. A full fetch can infer deletion of a record it actually received

Locations: [ingest result](../../aggregato/sync/dispatch.py#L617),
[full-run guard](../../aggregato/sync/dispatch.py#L1041),
[stale-item deletion](../../aggregato/ingest/writer.py#L96).

`_ingest()` returns failed counts, but `_apply_full_run_guards()` neither receives that count nor
checks `outcome.failures`. The sanity count includes failed records. A rejected existing item keeps
its old `last_seen_at`, making it look absent to `infer_deletes()`. The reproduction feeds the guard
a successful full outcome with an observed failed record and gets permission to tombstone it.

Delete inference is opt-in, and current bundled schemas do not expose this setting. This is an
implemented path relevant to drop-ins and future providers, not default deletion on every sync.

**Fix:** the shortest safe rule is to skip inferred deletion whenever normalization or ingestion
fails for any record. A more selective rule needs a separate validated set of observed native IDs.
Do not use successful ingestion as the definition of source presence.

**Acceptance:** an observed-but-rejected existing record survives a full run; genuine absence is
deleted only after an otherwise complete, sane, opted-in full run.

### R6. Creator split corrections do not survive the writer

Locations: [split](../../aggregato/ingest/split.py#L18),
[creator resolution](../../aggregato/ingest/resolve_creator.py#L37),
[credit upsert](../../aggregato/ingest/writer.py#L272).

Split moves selected credits to a new creator. The next sync resolves the original name/identifier
through the old creator's aliases and inserts another credit for that creator. The moved credit
survives, but the incorrect association returns alongside it. The reproduction shows one credit
becoming two after split and resync. The existing durability test covers manual **work** links only.

**Fix:** persist authoritative manual credit resolution, and consult it before automatic matching.
Use an existing stable source-credit key if possible; otherwise document a forward migration.
Preserve the correction across replay and include its reversal in undo.

**Acceptance:** split → resync → replay retains the corrected association without recreating the
old credit. Undo restores automatic behavior deliberately.

### R7. The optional backend SPA handler serves files outside its root

Location: [SPA fallback](../../aggregato/main.py#L212).

The fallback constructs `static_dir / path`, tests `is_file()`, and serves the candidate without
checking its resolved containment. With `AGGREGATO_STATIC_DIR` configured, the reproduction requests
`/%2e%2e/outside-static.txt` and receives a dummy file outside that directory. Public read-only mode
makes this reachable without credentials. Other files readable by the API process are at risk.

The normal Compose frontend uses Nginx and does not set the backend static directory. That narrows
the default exposure but leaves a supported alternate serving path unsafe.

**Fix:** delete backend SPA serving if Nginx is the supported deployment, or enforce resolved path
containment and exclude API paths from fallback. Keep symlinks and encoded traversal in the check.

**Acceptance:** encoded traversal, absolute paths, and escaping symlinks cannot return outside files;
normal assets and SPA routes still work.

### R8. Live updates do not work through the shipped proxy configuration

Locations: [Nginx](../../docker/nginx.conf#L15),
[Vite proxy](../../frontend/vite.config.ts#L20),
[sync stream](../../frontend/src/api/syncStream.ts#L67).

Nginx sets HTTP/1.1 but never forwards `Upgrade` or `Connection` for WebSockets. Those headers need
explicit forwarding, as documented by [Nginx](https://nginx.org/en/docs/http/websocket.html).
Vite's API proxy also lacks `ws: true`. Direct ASGI WebSocket tests bypass both proxies, so they
cannot establish that the published deployment works. Sync's HTTP fallback can hide the failure;
the now-playing socket has no HTTP fallback by contract.

**Fix:** add the native proxy configuration for WebSocket upgrades and an appropriate idle timeout
or heartbeat. Enable development WebSocket proxying. No application event framework is needed.

**Acceptance:** an authenticated browser receives an initial and changed snapshot through the
published Compose port, survives idle periods, and reconnects without an authentication loop.

### R9. Recovery is not safe with two workers

Locations: [worker startup](../../aggregato/worker.py#L61),
[recovery](../../aggregato/sync/scheduler.py#L316),
[atomic claim](../../aggregato/sync/scheduler.py#L285).

Every worker startup recovers **every** provider marked syncing, without checking an owner, lease,
or heartbeat. Starting a second worker closes a live first worker's run as failed, releases its
provider, and makes it claimable again. The reproduction demonstrates this transition using an
active run. Conditional claim protects two actors arriving simultaneously; it does not protect
active work from startup recovery.

Compose normally launches one worker, so this is conditional on overlap or another launch.
Comments that imply multiple schedulers are safe are broader than the implemented guarantee.

**Fix:** for the current single-user deployment, enforce a process-lifetime singleton before
recovery. If multiple hosts/workers are a real requirement, add durable ownership, expiry, and
conditional finalization instead. Reuse existing locking patterns; do not build distributed
scheduling merely to publish a single-worker app.

**Acceptance:** a second worker cannot recover or finalize a live owner's run. A dead owner's run
remains recoverable. The regression must use an actual overlap, not only sequential claim calls.

### R10. Shutdown requests a drain but does not await it

Locations: [signal handling and cleanup](../../aggregato/worker.py#L86),
[scheduler stop](../../aggregato/sync/scheduler.py#L661).

The signal handler launches `scheduler.stop()` in a detached task. `stop()` sets the event before
awaiting active runs. The main `run_forever()` can therefore return immediately, after which
`serve()` disposes the engine and exits without awaiting that stop task. `asyncio.run()` cancels
remaining tasks. This contradicts the nearby comment promising in-flight runs finish on restart.

**Fix:** use the signal to request stopping, then await the drain in the owned shutdown path before
disposing the engine. Choose and document a bounded grace period compatible with container shutdown.

**Acceptance:** stop during child execution and ingestion; completion or explicit cancellation is
recorded before disposal, children are reaped, and restart does not lose a checkpoint.

### R11. PostgreSQL support does not cover the Settings/backup journey

Locations: [storage accounting](../../aggregato/api/routes/settings.py#L77),
[JSON column type](../../aggregato/db/types.py#L39),
[export rejection](../../aggregato/export.py#L23),
[Settings copy](../../frontend/src/views/Settings.vue#L88).

Storage accounting emits `length(raw_payload)` where PostgreSQL stores JSONB. The documented
[`length` overloads](https://www.postgresql.org/docs/current/functions-string.html) operate on
strings and related scalar types; this query lacks a text cast. This is a traced PostgreSQL failure,
not a database execution result from this review. It also measures characters while reporting bytes.
Export explicitly raises for any non-SQLite backend, although the UI and site offer a portable
archive without that limitation.

**Fix:** cast JSON explicitly for a documented estimate, and distinguish character estimates from
storage bytes. Add Settings to the PostgreSQL smoke test. Expose backup capability and either
implement a PostgreSQL backup format or document the operator's PostgreSQL backup procedure.

**Acceptance:** Settings loads and saves on both supported databases; export availability and error
messages reflect the backend. The site cannot promise an always-available download until that holds.

### R12. A sufficiently large schema replay repeatedly fails before doing any work

Locations: [unbounded stale selection](../../aggregato/ingest/normalize_replay.py#L16),
[request limits](../../aggregato/sync/runner.py#L60),
[replay dispatch](../../aggregato/sync/dispatch.py#L554).

Every stale payload is loaded into one list and sent in one request. The runner rejects more than
100,000 replay records or more than 128 MiB of request JSON. Once an archive crosses either limit,
a normalization version bump cannot progress: each attempt constructs the same oversized request,
fails, and retries. This is especially relevant to years of listens in an app designed for 1M+
entries. A stdout limit is useful containment but is not replay pagination.

**Fix:** read and replay bounded batches of stale provider items, retiring/replacing each safely as
in R4. Advance per-item schema versions only after a successful commit. Keep payload and stdout
limits; chunking is the missing part.

**Acceptance:** an archive larger than one request limit completes replay across batches, resumes
after interruption, and preserves rejected records for another attempt.

## Other correctness, operation, and UX findings

| Finding | Severity / confidence | Location and recommended change |
|---|---|---|
| C1. Stale responses overwrite new requests | Medium / traced | [`useRequest` and `usePaged`](../../frontend/src/api/useApi.ts#L32) lack a request generation check. Apply filter A, then B; resolve B before A, and A can append to or overwrite B. The same helper feeds route-dependent Work/Creator views. Add one shared generation guard, including loading/error updates; test with deferred promises. |
| C2. Mutations reject without user-visible feedback | Medium / traced | [`Work`](../../frontend/src/views/Work.vue#L33) and [`Creator`](../../frontend/src/views/Creator.vue#L21) await merge/split/undo with no error catch or pending state. A 409 safe-undo refusal is invisible and double submission is possible. Reuse the existing problem presentation and disable each action while pending. |
| C3. Migrations create full backups on every ordinary restart | Medium / traced | [`upgrade_to_head`](../../aggregato/db/migrate.py#L82) always calls backup, even at head. API and worker each call it, creating two copies during a normal boot. There is no backup retention in cleanup. Under the migration lock, check whether an upgrade is needed; back up only then and document retention. |
| C4. Temporary export descriptor is leaked | Medium / traced | [`mkstemp`](../../aggregato/export.py#L29) discards its open file descriptor. Close it or use a managed temporary file. Exceptions before StreamingResponse cleanup also leave ZIP paths behind. Test failed creation and repeated exports. |
| C5. Retention label overstates what is retained/deleted | Medium / traced | [`cleanup`](../../aggregato/db/retention.py#L130) uses `raw_payload_retention_days` only for resolved failure rows. Provider-item payloads remain indefinitely. Rename/explain this setting or implement the documented policy without removing replay sources unexpectedly. |
| C6. Restoring a malformed archive leaves a half-restored target | Medium / traced | [`restore_archive`](../../aggregato/export.py#L48) writes the database and images before parsing configuration JSON. Bad JSON or a later extraction failure leaves a target that the next attempt refuses. Validate first, extract to a staging directory, and publish the finished restore atomically. Keep traversal checks. |
| C7. Weekly statistics disagree by backend | Medium / traced | [`_bucket`](../../aggregato/api/routes/stats.py#L80) uses SQLite `%Y-W%W` but PostgreSQL ISO `IYYY-WIW`. Around New Year they produce different week/year keys. Choose one calendar definition and test boundary dates on both dialects. |
| C8. Form submission bypasses native validation | Medium / traced | [`Providers`](../../frontend/src/views/Providers.vue#L394) places Save outside SchemaForm as `type=button`, so required inputs do not block that click. Defaults initialized inside [`SchemaForm`](../../frontend/src/components/SchemaForm.vue#L28) are not emitted until an input changes. Use a real form submit, and ensure initial defaults and nullable scalar types reach the parent. |
| C9. Provider checks finish but the card can keep saying pending | Medium / traced | [`Providers`](../../frontend/src/views/Providers.vue#L113) reloads the list immediately after enqueue. The live snapshot contains normal syncs rather than diagnostic results, and `last_check` comes from the separate list. Refresh a pending check until terminal; do not poll the whole page indefinitely. |
| C10. Global action state is misleading | Low / traced | [`Providers`](../../frontend/src/views/Providers.vue#L56) has one `busy` string but disables only that provider. Actions on two cards overwrite one another's busy/notice state. Either disable all actions while one runs or store pending state by provider. |
| C11. Public users repeatedly attempt protected sockets | Low / traced | [`useSyncStream`](../../frontend/src/api/syncStream.ts#L67) reconnects regardless of public session mode, although public WebSockets are refused. Use HTTP status refresh for public sessions; reconnect authenticated sockets only. |
| C12. Read-only controls flash or reappear on session probe failure | Low / traced | [`readonlyAccess`](../../frontend/src/api/session.ts#L14) starts false and resets false on error. The server still enforces permissions, but users see actions that cannot work. Represent unknown session state and show write controls only after confirmed operator access. |
| C13. Date filter excludes fractional timestamps at day's end | Low / traced | [`Log.query`](../../frontend/src/views/Log.vue#L62) uses `23:59:59Z` as an inclusive upper bound. `23:59:59.500Z` is excluded. Prefer an exclusive next-day boundary and document whether filters use UTC or the viewer's timezone. |
| C14. Outbound requests advertise the wrong release | Low / traced | [`__version__`](../../aggregato/__init__.py#L3) remains `0.1.0` while the project is `0.2.5`. [`USER_AGENT`](../../aggregato/providers/http.py#L53) uses that stale value, and the release script does not bump it. Derive the version from installed package metadata, or include this file in the existing release checks and rollback. Keep the API contract version separate. |

## Code smells and simplification opportunities

The best cuts remove duplicated state or false promises. Keeping validation, savepoints, closed
vocabularies, CSRF, keyset pagination, migration history, and process containment is justified.

| Priority / tag | What to cut or simplify | Replacement and limits |
|---|---|---|
| High / `shrink` | [`RunPlan`](../../aggregato/sync/dispatch.py#L69) duplicates almost every field of `RunRequest`, then converts tuple → list and copies fields for each branch. | Use `RunRequest` directly with `dataclasses.replace`; there is already a suitable record. About 25–40 lines could go without another abstraction. |
| High / `shrink` | [`RunFinalization.persist`](../../aggregato/sync/dispatch.py#L121) recreates the same wide keyword list accepted by [`release`](../../aggregato/sync/scheduler.py#L411). | Make one typed finalization record the persistence interface, or remove the wrapper. Merely adding a record above the old parameter soup did not simplify the interface. Preserve explicit transaction ownership. |
| High / `shrink` | [`build_dispatch`](../../aggregato/sync/dispatch.py#L164) selects job/mode/cursor/lineage with nested conditional expressions, then passes the same state through `run_once` and `_run_opened`. | Select the operation with a short explicit branch; pass the already-built request and run context once. Keep the job leases distinct while their file/payload ownership differs. |
| High / `shrink` | [`write_batches`](../../aggregato/ingest/writer.py#L149) copies the entire creator memo per record and updates the entire copy after success. | Stage only newly resolved keys for the record, publishing that delta after savepoint commit. With one new creator per record, full-map copying/updating sums to quadratic work. Keep rollback isolation; do not remove it to save lines. |
| Medium / `delete` | Backend static hosting duplicates the always-on Nginx frontend and contains R7. | Remove `_mount_frontend` and `static_dir` if alternate backend SPA hosting is not a supported product feature. Otherwise fix and document both paths. Approximately 35–55 lines possible, plus stale documentation. |
| Medium / `shrink` | [`providers.py`](../../aggregato/api/routes/providers.py) is 1,462 lines: routes, discovery reconciliation, uploads, schema interpretation, redaction, and status initialization. | Move only the reused config-schema validation/redaction into a provider contract module. Keep routes recognizable. Splitting files alone is not a simplification; reducing repeated interpretations is. |
| Medium / `shrink` | API, dispatch, and now-playing interpret setting secrecy independently. | Share the explicitly supported flat-schema projection. Discovery should reject unsupported configuration shapes once. Keep deny-by-default exposure rules; do not replace them with field-name heuristics. |
| Medium / `shrink` | [`manifest.py`](../../aggregato/providers/manifest.py) repeats provider declarations, schemas, intervals, scales, and field prose. | Retain safe static discovery. Use a metadata-only module that both host and provider consume, or generate static manifests during maintenance/build. Choose one; do not import provider implementation during discovery. The parity test helps, but does not remove the editing burden. |
| Medium / `native` | Frontend types are maintained separately from YAML and FastAPI schemas. | Generate TypeScript declarations as a committed artifact using a dev-only tool, or add focused shape parity checks. The current `ProviderCapability` already lacks `now_playing`. Keep the small fetch wrapper; a generated runtime SDK is unnecessary. |
| Medium / `delete` | [`boot_jitter`](../../aggregato/sync/scheduler.py#L565), its RNG, and constant have no production caller. | Either apply it at the one intended initial-schedule transition or delete the method and claim. Keeping an unused feature makes the documentation wrong. |
| Medium / `yagni` | [`ProviderContext.state`](../../aggregato/providers/base.py#L103) promises a persisted mutable store, but normal dispatch does not load it and the protocol has no state-write message. | No bundled provider currently uses it. Remove/reserve the promise until a real provider needs it, or implement one explicit state update path. Passing a mutable dict into another process does not persist mutations. |
| Low / `shrink` | [`downloadArchive` and import upload](../../frontend/src/api/client.ts#L299) bypass the shared request error/auth handling. | Let the existing wrapper accept FormData and a response decoder; two small options suffice. Preserve JSON defaults. This also fixes inconsistent 401 routing without a client framework. |
| Low / `native` | [`notice.includes('Undo:')`](../../frontend/src/views/Work.vue#L99) and `split(': ')[1]` use display copy as program state. The Work handler adds a zero-delay timer to create the second message. | Store `undoId: number | undefined` separately and render it directly. Delete the timer. Text edits and translation should not change undo behavior. |
| Low / `shrink` | [`Creator.vue`](../../frontend/src/views/Creator.vue#L21), SplitDialog, and several templates compress entire async functions/forms onto one line. | Expand only these dense blocks with normal formatting. Short line count is not the same as short reading time. Add a formatter/check using an existing or dev-only tool if the team wants enforcement. |
| Low / `shrink` | Icon SVG markup and operational date formatting repeat across views. | Extract an existing repeated icon only after selecting a small stable set; reuse `LoggedAt`. Do not add an icon registry, UI framework, or date library for this cleanup. |
| Low / `delete` | Empty `NOT_YET_IMPLEMENTED` plus tests for its empty exemption machinery in [`test_openapi.py`](../../tests/contract/test_openapi.py#L39). | Assert equality of served and contract paths/methods directly now that the app is implemented. About 25–45 lines of historical scaffolding can go. |
| Low / `yagni` | Reserved push/scrape/deletion features have no bundled consumers. | Keep required acquisition-policy enforcement. Avoid expanding unused runtime branches until a provider needs them; clearly mark reserved capabilities as unsupported rather than building a plugin framework. |
| Low / `native` | [`update_settings`](../../aggregato/db/retention.py#L69) uses SELECT then INSERT/UPDATE despite a host upsert helper. | Reuse `upsert_stmt` for the unique settings key. This removes a round trip and the empty-table concurrent-insert race. |

The scheduler semaphore duplicates the concurrency count maintained in `_running` under the public
polling route. It may be removable once concurrency ownership is made explicit; do not remove it
while tests or other callers depend on `_guarded` directly.

**Estimated easy deletion:** roughly **85–150 lines**, principally duplicate invocation records,
unused jitter plumbing, and empty pending-contract scaffolding. Another **35–55 lines** are optional
if backend SPA serving is retired. These are estimates, not measured savings from an applied diff.
No production dependency is clearly removable without narrowing a currently used feature.

## Performance and test credibility

1. **The benchmarks do not enforce the declared product budgets.**
   [Query latency](../../tests/bench/test_query_latency.py#L21) uses 20,000 rows, a single timing,
   and a simplified SELECT rather than the actual filtered API. Its descending “deep” cursor skips
   roughly the newest 100 rows; it is not at the far end. Absolute checks of 200/300 ms do not test
   p95 at 1M, the 20% deep-page relation, or the 10% baseline regression rule. The
   original `tests/bench/test_budgets.py` declaration test checked JSON declarations, not ingest
   throughput. Record actual API and writer measurements before claiming the published budgets.

2. **Search materializes the entire matching set before fetching one page.**
   [Title/review helpers](../../aggregato/api/queries.py#L349) fetch every matching ref ID into
   Python; [entries](../../aggregato/api/routes/entries.py#L129) builds large `IN` lists from them.
   Broad searches can exceed bind parameter limits or allocate far more than a page of data. This
   is a scaling risk, not a measured regression here. Keep the previously removed match cap removed;
   express matching as a database subquery/join with an explicit portable ID representation instead.

3. **Protocol streaming is not streaming ingestion.**
   [The runner](../../aggregato/sync/runner.py#L68) retains every decoded raw+normalized record and
   response snapshot; [dispatch](../../aggregato/sync/dispatch.py#L617) starts ingestion after the
   child finishes. Serialized byte limits do not bound expanded Python object memory. Measure peak
   RSS and write-lock duration for large listens imports. If they fail the target hardware budget,
   commit bounded checkpoint-aware batches; do not add an event bus or broker just for buffering.

4. **Concurrency tests can look stronger than they are.**
   [Scheduler unit tests](../../tests/unit/test_scheduler.py#L50) wrap a synchronous database in async
   methods. `asyncio.gather` there does not establish true SQL overlap. The dedicated PostgreSQL
   smoke is useful complementary coverage, but neither replaces two-worker recovery and shutdown
   tests. Add targeted tests for those actual transitions.

5. **The API contract test mostly verifies the endpoint inventory.**
   [OpenAPI tests](../../tests/contract/test_openapi.py) compare paths, methods, selected response
   metadata, and enums. They do not comprehensively compare parameter/body/response schemas or the
   handwritten TypeScript mirror. The claim that the harness detects schema drift in both directions
   is too broad. Add focused schema checks around changed contracts or generate committed types.

6. **Proxy and browser journeys are absent from passing test evidence.**
   Test configure → credential check → enable → sync → live update → failed correction → undo,
   public/read-only navigation, and export restrictions through the real frontend deployment.
   Keep these deployment checks outside the socket-blocked offline pytest suite. Accessibility
   verification still needs keyboard, zoom, focus after navigation, and screen-reader passes.

## Scheduler and TaskIQ

### What deserves to stay

The requirements call for outcome-driven intervals, disabled/degraded state, per-provider exclusion,
durable manual requests, checkpoints, retries with lineage, isolated provider children, and a
single-user SQLite installation without a mandatory broker. A small database due-queue is a
reasonable implementation of those requirements.

The current implementation is much larger than that description:

| Module | Lines | Responsibility |
|---|---:|---|
| `scheduler.py` | 665 | Admission, claims, recovery, release, concurrency, polling |
| `dispatch.py` | 1,111 | Operation selection, replay, execution, ingest, deletion guards, finalization |
| `jobs.py` | 259 | Import/replay leases and lifecycle |
| `retry.py` | 177 | Domain retry decisions |
| `now_playing.py` | 299 | Separate transient polling lifecycle |

These are scope counts, not proof that every line is unnecessary. Nevertheless,
[`research.md`](../research.md#L24) still calls the scheduler “about 40 lines” and says observability
comes free. That rationale no longer describes the maintenance cost.

### What TaskIQ would and would not remove

TaskIQ supports dynamic schedules and dispatches work through a broker; its scheduler is a separate
command, and its documentation warns about duplicate dispatch from multiple scheduler instances.
Those facts do not establish safe application claims or recovery for Aggregato.
[TaskIQ scheduling documentation](https://taskiq-python.github.io/guide/scheduling-tasks.html).

Its InMemoryBroker is intended for local development, not durable production delivery. Redis,
RabbitMQ, and NATS brokers are available. PostgreSQL schedule sources also exist, so “TaskIQ always
requires Redis” would be an inaccurate dismissal.
[Brokers](https://taskiq-python.github.io/available-components/brokers.html),
[schedule sources](https://taskiq-python.github.io/available-components/schedule-sources.html).

| Concern | Keep and simplify current worker | Introduce TaskIQ |
|---|---|---|
| Default SQLite / no additional service | Already fits | Requires choosing a production broker and schedule source that actually fit this deployment |
| Dispatch and task execution mechanics | Owned locally; repair singleton/shutdown semantics | Library supplies substantial infrastructure |
| Application claims, cursors, run rows, inferred deletes | Remain application code | Remain application code |
| Per-run provider isolation | Existing supervised child | Preserve the child boundary inside the task; ordinary task workers are not that boundary |
| Dynamic retry schedule | One database field updated with run outcome | Reconcile application state with the schedule source; prevent crash-window divergence |
| Distributed/multiple-host workers | Requires new ownership semantics | Potential benefit, but duplicate dispatch and idempotent admission still need explicit treatment |
| Scope before publication | Fix defects and reduce overlapping state | Migration, dependencies, topology, lifecycle tests, and documentation changes |

**Recommendation:** keep the database schedule for this publication, fix R3/R9/R10, and shrink the
duplicate request/finalization structures. Keep one durable source for whether work is runnable.
Document single-worker ownership and enforce it. Update the old research rationale.

Adopt TaskIQ when there is an explicit need for distributed workers, an existing broker deployment,
or enough measured lifecycle maintenance to justify its integration. This recommendation is an
assessment of Aggregato's requirements, not a claim that custom scheduling is inherently better.

If a TaskIQ migration is chosen, make it reviewable by proving all of these before switching:

- One operator request retains the same lineage through retry and restart.
- Simultaneous delivery, overlapping workers, and startup recovery cannot produce two active owners.
- Disabling or reconfiguring a provider cannot be undone by a stale completion.
- Partial child output persists safely and advances only an ingested, flushed checkpoint.
- Auth/blocked/structure failures produce no automatic retry.
- Import/replay work survives worker death and cannot strand a file or payload.
- A SQLite installation retains its documented deployment requirements, or the change is explicit.

Changing the package used to wake a worker without proving those conditions is not a scheduler fix.

## Text, comments, and LLM-style residue

The issue is observable writing quality, not proof of who wrote a sentence. Em dashes, careful
documentation, and clear lists are not themselves evidence of an LLM. The actual problems are
broken references, stock self-justification, planning-template remnants, contradictory statements,
and prose that promises properties the code does not implement.

| Location / current text | Problem | Human-facing replacement or action |
|---|---|---|
| [`architecture.md`](../architecture.md#L185): “what 's containment requires and 's single-command deployment allows” | Broken reference cleanup left unreadable possessives. | “The API and worker run separately so provider failures do not stop API requests. Compose starts both services.” |
| [`research.md`](../research.md#L11): “## — Scheduling”; “No `NEEDS CLARIFICATION` remains” | Empty decision label and planning-template status. | “Scheduling” and an ordinary dated decision paragraph; delete the status sentence. |
| [`requirements.md`](../requirements.md#L3): feature branch, “Input”, “User Scenarios & Testing *(mandatory)*”, Draft | Internal generation template presented as current product authority. | Keep scenarios and constraints. Replace the header with scope/version/status useful to contributors, and remove template instructions. |
| [`architecture.md`](../architecture.md#L191): “Complexity Tracking”, “Violation”, “Requested directly by the operator for this plan” | Discusses compliance with an absent planning template. | Replace with a short tradeoffs section about the shipped architecture and current costs. |
| [`architecture.md`](../architecture.md#L19): TypeScript 5.x; Vitest covers one form | Factually stale. | TypeScript 6.x in the current manifest; describe the actual frontend test coverage without promising a fixed count. |
| [`architecture.md`](../architecture.md#L146): “generated-from-openapi client” | Contradicts handwritten `types.ts`. | “Typed fetch client with handwritten contract types,” until generation is implemented. |
| [`main.py`](../../aggregato/main.py#L3): an SPA “physically cannot ... reach a private endpoint” | Incorrect architectural claim; browser JavaScript can call any reachable endpoint. | “The frontend uses the documented API. Authentication and authorization are enforced by the server.” |
| [`base.py`](../../aggregato/providers/base.py#L61): frozen context prevents replacing/bypassing the HTTP client | A frozen dataclass does not sandbox arbitrary Python. | “Providers must use the host client. Bundled providers are checked by conformance and import rules. Drop-ins require source review.” |
| [`worker.py`](../../aggregato/worker.py#L3): “put ingest CPU in the request path against . Two processes make 's containment ...” | Orphaned references and an overlong defense of the architecture. | “Runs the worker separately from the API and supervises provider syncs.” Link the maintained architecture once. |
| [`scheduler.py`](../../aggregato/sync/scheduler.py#L613): an exception in a spawned run “would kill the loop” | A task exception does not automatically kill the separate poll loop. | “Record dispatch failures and release the provider so other runs can continue.” |
| [`writer.py`](../../aggregato/ingest/writer.py#L524): no-ID entries use a SELECT-and-skip path | Explains code that no longer exists. | “Both event-ID and fallback keys use database unique indexes and upserts.” |
| [`client.ts`](../../frontend/src/api/client.ts#L267): enable uses PATCH | The function actually sends POST to `/enable` or `/disable`. | State the actual operations; delete the invented contract inference. |
| [`useApi.ts`](../../frontend/src/api/useApi.ts#L82): a failed local archive page would “hammer a provider” | Confuses the local API with source acquisition; transient failure does not invalidate an unchanged cursor. | “Expose the request error and allow retrying the page.” Preserve cursor state on failure. |
| [`Providers.vue`](../../frontend/src/views/Providers.vue#L121): full resync is “the only way” to apply a normalization fix | Automatic stored-payload replay already exists. | “Fetch all history currently exposed by the provider, ignoring its saved fetch position.” Explain replay separately. |
| [`Settings.vue`](../../frontend/src/views/Settings.vue#L88): “Secrets and active sessions are excluded” | Security promise contradicted by R1. | Fix export before retaining this sentence. Text alone cannot repair the promise. |
| [`site/index.html`](../../site/index.html#L568): client “cannot bypass” limits; crash “never touches the others” | Overstates enforcement and failure isolation, especially for drop-ins and resource exhaustion. | “Bundled providers use a host-managed HTTP client and run in separate child processes with timeouts.” |
| [`site/index.html`](../../site/index.html#L487): download “at any time,” SQLite “or Postgres” | PostgreSQL download is explicitly unsupported. | Describe SQLite download and link the PostgreSQL backup procedure. |
| [`README.md`](../../README.md#L44): Goodreads means an operator-downloaded import; StoryGraph uses the same shape | Contradicts the shipped Goodreads RSS-only acquisition path. | Remove the Goodreads analogy; move candidate sources to the roadmap. |
| [`README.md`](../../README.md#L92): show latest run before the first run | A new user has no result to inspect; credential check is now available. | Configure → Check provider → Enable → Sync now → inspect result. Use exact current button labels. |
| Repeated “honest”, “structural rather than disciplinary”, “the point is”, “nothing more general”, “only one”, “exactly what ... requires” | Commentary repeatedly argues the design instead of explaining code. | Keep comments about non-obvious constraints, edge cases, and transaction ordering. Delete advocacy and restatements of obvious control flow. |
| Comments/docstrings containing `requires .`, `forbids —`, `research.md ,`, empty `(, ...)`, milestone M1/M2 labels | Visible remnants of deleted requirement identifiers. | Rewrite the sentence; do not strip another identifier mechanically. Preserve useful document links without dangling punctuation. |

Example publication copy, after the release blockers are fixed:

> Aggregato collects your media activity from supported services into a database you control.
> Browse listens, watches, reads, ratings, and reviews in one web interface. It is single-user and
> self-hosted. Providers are opt-in; a new installation makes no provider requests. Ratings retain
> their original scales, and dates retain the precision supplied by each service.

Example architecture summary:

> The API serves archive queries and queues operator requests. A separate worker selects due work
> from the database. Each provider runs in a supervised child process; the parent validates its
> output and writes it. SQLite is the default store. PostgreSQL is optional. Provider isolation
> limits crashes and hangs but does not sandbox unreviewed Python code.

Keep the specific descriptions of rating scales, date precision, ownership, and opt-in acquisition.
They explain the product. Avoid replacing them with generic claims such as “seamless,” “robust,”
or “powerful.” Keep internal reviews as clearly dated records, outside the primary onboarding path.

## Publication package and order of work

### First: make the advertised behavior true

1. Fix R1/R2 and add operator-only boundaries for backups and raw diagnostics.
2. Fix R3 and exercise the full failure-to-next-poll transition.
3. Fix R4/R5 with record-level replacement atomicity and safe full-run inference.
4. Fix R6/R7, then verify creator correction durability and static-file containment.
5. Fix proxy upgrades and shutdown; enforce worker ownership before recovery.
6. Add bounded replay, correct PostgreSQL Settings, and state the backup limitation accurately.

Each behavioral fix needs a deterministic regression test. Migrations remain forward-only and need
previous-revision survival coverage. Preserve the acquisition policy and the existing process,
validation, and import boundaries.

### Then: reduce review cost

- Reuse `RunRequest`; simplify finalization and operation selection without introducing a workflow
  framework or base classes.
- Remove unused jitter and empty pending-implementation scaffolding, or finish the claimed behavior.
- Stage memo deltas; measure the actual writer and API before broader performance changes.
- Give mutation handlers errors/pending state and use structured undo state.
- Align static metadata, API types, and the small supported provider config schema.

### Finally: prepare the public repository

| Deliverable | Current gap | Concrete publication action |
|---|---|---|
| License | No repository LICENSE file or project license metadata found | Maintainer chooses the license; add its full text and package metadata. Do not guess a license on the maintainer's behalf. |
| Security reporting | No SECURITY.md found | Add a reporting route the maintainer actually monitors and a brief supported-version policy. |
| Release notes | No CHANGELOG found | Provide concise release notes listing bundled sources, acquisition limits, backup support, and known limitations. Do not copy internal phase/task records. |
| Version consistency | Runtime version and outbound User-Agent remain `0.1.0` | Use one package-version source or cover all release version files in the bump, rollback, and verification steps. |
| README | Candidate roadmap crowds onboarding; stale Goodreads analogy and first-run steps | Lead with current functionality, supported providers, install, credential check, backup/restore, and troubleshooting. Link the roadmap afterward. |
| Screenshots | Landing page and README contain older screenshots/labels | Re-capture with synthetic demo data after fixes, verify labels match the release, and use stable repository assets. Confirm no credentials or personal histories are visible. |
| Provider limitations | Feeds/recently-played surfaces are presented alongside full history | Explain the history window each source actually exposes. A “full resync” cannot recover data the source no longer supplies. |
| Operator guide | Public read access currently includes operational data; backup claims too broad | Define what public/read-only viewers may see, document SQLite vs PostgreSQL backup, and describe update/restore steps. |
| Accessibility evidence | Dated implementation review without a deployed browser verification record | Verify keyboard navigation, form errors, focus after navigation, zoom, and status announcements on the final build. Inline forms need usable labels; a component named Dialog does not prove modal behavior. |
| Reproducible release | Local installed versions differ from locks | Run clean `uv sync --all-extras --dev` and `npm ci`, all gates, PostgreSQL smoke, and actual image/deployment checks in CI. Record results for the release commit. |

### Release acceptance

- The reproduced defects no longer reproduce, with regression tests for the desired behavior.
- Ruff format/check, mypy, import-linter, offline non-benchmark tests, conformance, frontend types,
  unit tests, and production build pass on the locked installation.
- PostgreSQL smoke covers Settings and the documented backup path/limitation.
- A clean Compose instance completes the first-provider journey with actual proxy WebSockets.
- Restore, restart during sync, public/read-only access, and manual correction/resync are verified.
- Declared performance claims have measurements at their stated scale, or are labeled targets.
- README, site, UI copy, and architecture describe the same shipped behavior.
- License, reporting instructions, and release notes are present.

No scheduler migration, new broker, ORM, repository layer, UI framework, or general plugin workflow
engine is required to meet these acceptance conditions.
