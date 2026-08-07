# Scheduler and runtime reliability findings

The scheduler has a clear conceptual pipeline, but several state transitions are split across
unconditional reads and writes. The result is that stale API/scheduler observations can overwrite
newer state, and host-side exceptions are not classified as run outcomes.

## R-01 — Misconfigured providers remain enabled and due (High)

The provider configuration route detects a validation error but persists the requested \`enabled\`
value before returning its 422. See [providers.py:633](../../aggregato/api/routes/providers.py:633)
and [providers.py:713](../../aggregato/api/routes/providers.py:713). The due query excludes disabled
and syncing providers but not misconfigured providers; see
[scheduler.py:93](../../aggregato/sync/scheduler.py:93).

Impact:

- An operator receives an error while the database can contain \`enabled=true\`,
  \`status=misconfigured\`.
- The scheduler can claim the provider anyway.
- \`sync_now\` checks enabled/syncing, resets the status to idle, and can queue a known-bad provider.

Fix:

- Make an invalid provider block atomically persist \`enabled=false\` and a non-runnable status.
- Exclude \`MISCONFIGURED\` explicitly in due selection and claim predicates.
- Make \`sync_now\` reject misconfigured providers unless an explicit force path is intended.
- Add a regression test that submits invalid config and asserts the persisted state, due queue, and
  manual trigger all remain non-runnable.

## R-02 — Admission and status transitions race (High)

The due list is read first and \`claim\` later checks only provider identity/status rather than
revalidating enabled, misconfigured status, capability, and due time. See
[scheduler.py:135](../../aggregato/sync/scheduler.py:135). The API sync path reads status and then
performs a later unconditional status update; see
[providers.py:276](../../aggregato/api/routes/providers.py:276) and
[providers.py:306](../../aggregato/api/routes/providers.py:306).

Impact:

- A provider disabled after due-listing can still be claimed.
- A provider can be claimed between the API's read and its status write, after which the API writes
  \`idle\` over \`syncing\`.
- Two schedulers or an API request can race on the same cursor and produce duplicate runs or cursor
  loss.

Fix:

- Use one conditional atomic update for admission:
  \`enabled = true AND status IN (idle,degraded) AND next_run_at <= now\`, plus capability checks.
- Check the affected-row count and create the run only after the claim succeeds.
- For PostgreSQL, use row locks or an atomic \`UPDATE ... RETURNING\`; for SQLite, use a transaction
  shape that cannot observe and then overwrite a stale status.
- Add deterministic two-actor race tests, not only sequential state tests.

## R-03 — Disabling during a run can be undone by rescheduling (High)

Disable updates provider state while a run is in flight, but dispatch release unconditionally writes
the normal post-run status/next-run values. See [scheduler.py:322](../../aggregato/sync/scheduler.py:322)
and [dispatch.py:455](../../aggregato/sync/dispatch.py:455).

Impact:

- An operator can disable a provider, see it disabled, and then have the completing run return it
  to an idle/scheduled state.
- The next run may occur despite the operator's explicit action.

Fix:

- Make release conditional on the provider still being in a runnable state, or preserve
  \`DISABLED\` in the release transition.
- Model state transitions with an expected version/generation and reject stale writers.
- Add “disable while child is running” and “disable while release is pending” tests.

## R-04 — Host failures can leave \`sync_runs\` permanently running (High)

The run row is opened before execution, but \`run_once\` has many host-side failure points: provider
loading, replay lookup, child spawn, database writes, and rescheduling. The runner converts child
failures into outcomes, but spawn or host exceptions can escape. The scheduler's broad guard releases
the provider without necessarily closing the run; see [scheduler.py:322](../../aggregato/sync/scheduler.py:322)
and [dispatch.py:210](../../aggregato/sync/dispatch.py:210).

Impact:

- A failed child spawn or database exception can leave an unbounded \`running\` row.
- Operator history and recovery logic disagree about whether work is in flight.
- Provider status and run status can diverge.

Fix:

- Wrap the entire dispatch after run creation in a host-failure finalizer that closes the run with an
  explicit internal error, records the exception safely, and releases/marks degraded atomically.
- If failure occurs before run creation, record a synthetic failed attempt or durable error state.
- Make startup recovery and normal exception handling use the same finalizer.
- Add tests that inject failures at every boundary after \`sync_runs\` insertion.

## R-05 — Cursor update and release are separate commits (High)

The dispatch path updates the cursor and releases the provider in separate transaction scopes. See
[dispatch.py:455](../../aggregato/sync/dispatch.py:455) and
[dispatch.py:480](../../aggregato/sync/dispatch.py:480).

Impact:

- Another scheduler can claim the provider after release but before the cursor commit and start from
  stale state.
- A crash between the commits leaves a released provider with an old cursor or a new cursor but no
  corresponding run outcome.

Fix:

- Commit cursor, run closure, provider status, retry decision, and next-run time as one state
  transition.
- Use an expected run/provider generation in the update predicate.
- Add crash-boundary tests with transaction failure injection.

## R-06 — Requested mode and lineage are not durable (High)

The API generates a lineage UUID and returns it, but only stores requested mode/next-run/cursor; the
dispatch path generates another lineage. See [providers.py:273](../../aggregato/api/routes/providers.py:273),
[providers.py:297](../../aggregato/api/routes/providers.py:297), and
[dispatch.py:207](../../aggregato/sync/dispatch.py:207). Retry decisions calculate a lineage but the
release/state schema does not carry it into the next attempt.

Impact:

- The caller's returned lineage does not necessarily identify the run it requested.
- Retries cannot reliably be grouped with the initial request.
- A requested full mode can be lost if the process fails after clearing/consuming the request but
  before a run begins.

Fix:

- Add a durable \`sync_requests\`/queue row containing request id, lineage, mode, attempt, and state,
  or persist pending lineage/mode in provider state with compare-and-swap semantics.
- Consume the request atomically with run creation.
- Make retry release write the next queue state and preserve lineage by construction.
- Add tests for API request → first run → retry lineage and for failure before child spawn.

## R-07 — Import-job claiming and recovery are incomplete (Medium/High)

Import selection and marking started are separate operations and the update does not appear to use a
claim predicate/row-count check; see [scheduler.py:120](../../aggregato/sync/scheduler.py:120).
Completion records only \`finished_at\`; exceptions leave the job started forever. The upload route
also resets provider status without checking for an active run; see
[providers.py:418](../../aggregato/api/routes/providers.py:418).

Impact:

- Two workers can process the same import.
- A crashed worker can permanently strand an import job.
- Upload-triggered import can race an ordinary sync and duplicate cursor/data work.

Fix:

- Claim with one conditional update and a lease/owner token.
- Add \`failed_at\`, error class/message, retry count, and recovery of expired leases.
- Reject or serialize import requests while a provider run is active.
- Use the same lineage/request queue as ordinary syncs where possible.

## R-08 — Non-poll providers can enter the due queue (Medium)

Due selection is based on enabled/status/time and does not clearly filter providers whose capability is
not pollable. See [scheduler.py:93](../../aggregato/sync/scheduler.py:93).

Impact:

- Push-only, export-only, or check-only providers can be scheduled as if they had incremental
  polling, producing confusing failures and retry churn.

Fix:

- Make scheduler eligibility a first-class capability predicate.
- Represent event-driven providers with an explicit “not scheduled” state rather than a null
  interval that happens to be due.
- Add tests for every capability combination.

## R-09 — Startup migrations have a multi-process race (Medium/High)

Both API and worker startup call migration upgrade, while worker comments acknowledge concurrent
migrations are unsupported. See [main.py:73](../../aggregato/main.py:73),
[worker.py:44](../../aggregato/worker.py:44), and
[migrate.py:52](../../aggregato/db/migrate.py:52).

Impact:

- Two processes can race on Alembic version/table DDL and SQLite backup creation.
- PostgreSQL deployments can see incompatible schema during startup.
- A failed migration can leave one process serving while the other exits.

Fix:

- Make one process the migration owner, or acquire a database-specific migration lock before both
  backup and upgrade.
- Use PostgreSQL advisory locking and an explicit SQLite exclusive/file lock.
- Add a two-process startup test and a PostgreSQL migration smoke job.

## R-10 — Clock injection is inconsistent (Medium/Low)

The scheduler and runner accept clocks, but API routes, image caching, retention, authentication,
and parts of import scheduling call \`datetime.now(UTC)\` directly. This makes time-dependent failure
paths hard to reproduce and weakens the repository's deterministic-test promise.

Fix:

- Put one host clock in application state and require it in routes/services.
- Keep monotonic time only for actual wall-clock timeout/rate pacing.
- Add tests for expiry, retry boundaries, retention, and same-timestamp races without sleeps.
