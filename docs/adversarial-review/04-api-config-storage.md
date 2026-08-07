# API, configuration, database, and storage findings

These findings are mainly contract drift and state-model weaknesses. They are less dramatic than
the provider boundary, but they create confusing operator behavior and make future fixes harder.

## A-01 — The credentials check endpoint does not check credentials (High product/contract bug)

The route reports the latest sync result and explicitly documents that an on-demand check is not
built. See [providers.py:315](../../aggregato/api/routes/providers.py:315) and
[providers.py:324](../../aggregato/api/routes/providers.py:324). It never calls the Provider Protocol's
check method.

Impact:

- The OpenAPI contract, README, and frontend say “verify credentials” or “credentials accepted/
  rejected,” while the implementation says “last run” or “not yet known.”
- Operators can believe credentials were validated when no check happened.
- Provider.check implementations are effectively dead in production.

Fix:

- Either implement a durable check-request job handled by the worker/child, returning 202 and a
  persisted check result, or rename the route/UI/OpenAPI to “show last run result.”
- Do not call provider.check in the API process.
- Add tests for never-run, successful-check, rejected-check, timeout, and read-only behavior.

## A-02 — Sync mode input can produce a 500 instead of a 422 (Medium)

The sync route accepts an untyped dict and tests membership directly:
[providers.py:264](../../aggregato/api/routes/providers.py:264). A JSON list or object supplied as
mode is unhashable and can raise TypeError while evaluating the set membership.

Fix:

- Define a Pydantic request model with Literal["incremental", "full"] and reject malformed JSON with
  the standard problem response.
- Add tests for null, list, object, integer, unknown string, and both valid modes.

## A-03 — Documented database configuration precedence is not actually wired (Medium)

The configuration module documents defaults ← YAML ← environment ← database overrides and exposes a
db_overrides argument, but API and worker startup call load_config without loading a database
override layer. See [config.py:1](../../aggregato/config.py:1) and the startup call sites.

Provider-specific UI settings are stored separately in providers.config and used by dispatch, so
there are two different notions of “database configuration.”

Impact:

- Operators and future contributors can edit an apparently supported override that production never
  reads.
- Export/restore expectations are ambiguous.
- A settings change can appear to succeed while startup continues using YAML/environment values.

Fix:

- Choose one source of truth: implement and test the database override layer, or remove it from the
  public contract and clearly document provider-state storage as a separate mechanism.
- Add a precedence matrix test for every layer and secret reference.

## A-04 — Provider settings redaction is best-effort rather than schema-safe (Medium/High)

The public settings filter inspects only limited inline JSON-schema shapes and uses a finite set of
names to recognize sensitive fields. See [providers.py:578](../../aggregato/api/routes/providers.py:578).
A provider schema using refs, allOf/anyOf, or a differently named secret field can bypass the filter.

Impact:

- A provider can accidentally expose an access token, client secret, private key, or session cookie
  through the API/UI.
- Redaction behavior changes when a schema is refactored without a security test.

Fix:

- Treat schema annotations such as writeOnly/secret as authoritative and resolve refs/compositions.
- Use a strict allowlist for fields returned publicly; do not attempt to infer secrets from names
  alone.
- Reject a provider schema containing unannotated secret-like fields or require an explicit review.
- Add tests for nested objects, refs, arrays, aliases such as access_token/client_secret/private_key,
  and provider-specific schemas.

## A-05 — Provider state is not guaranteed to exist before it is updated (Medium)

Several routes update provider_state with a plain update. If an existing provider row lacks its
state row, the update affects zero rows while the API returns success. This can happen after partial
initialization, a migration/import mistake, or a manually repaired database.

Impact:

- A provider can be reported enabled but never become due because next_run_at/requested_mode/cursor
  were not stored.
- The failure is silent and difficult to diagnose from the API response.

Fix:

- Enforce one provider_state row with a foreign key/unique invariant and create it in the same
  transaction as provider installation.
- Use an upsert and assert affected-row counts for every state mutation.
- Add a repair/startup invariant check that reports or reconstructs missing state safely.

## A-06 — Import queueing can overwrite an active provider state (Medium/High)

The import route checks only enabled, then stores the file and resets status to idle/next_run without
checking for syncing. See [providers.py:384](../../aggregato/api/routes/providers.py:384) and
[providers.py:425](../../aggregato/api/routes/providers.py:425).

This is the API-facing half of the scheduler race described in
[scheduler-runtime.md](./02-scheduler-runtime.md).

Fix:

- Use one admission function for ordinary sync, full sync, and imports.
- Reject or queue behind an active run and preserve the existing cursor/status.
- Return a durable request ID/lineage for the import rather than only an ephemeral UUID.

## A-07 — Cursor/pagination behavior has hidden caps and naming drift (Medium)

The keyset approach is good, but search filters have a hidden 500-ID cap and work details have fixed
embedded history limits. The runs route passes a started-at sort column while using a created-at
cursor name, which is confusing even if serialization currently works.

Fix:

- Make every cap explicit in OpenAPI and return a truncation/continuation signal.
- Use endpoint-specific cursor field names that match the actual sort column.
- Add boundary tests for null sort values, duplicate sort values, deleted rows, and >500 search
  matches.

## A-08 — Generic API error and payload surfaces need an explicit privacy policy (Medium)

Ingest-failure responses include raw payloads so operators can diagnose provider failures. That is
useful, but raw payloads may contain private notes, source URLs, identifiers, or data that was not
intended for every read-only token holder.

Fix:

- Document that the read-only token grants access to raw diagnostic payloads, or split archive-read
  and diagnostic-read privileges.
- Add payload-size limits and redaction rules for known credentials/headers before persistence.
- Ensure logs never duplicate raw payloads or resolved secrets.

## A-09 — Configuration models rely on mutable literal defaults (Low/Medium)

ProviderConfig uses settings: dict = {}, and the top-level config uses similar mutable defaults in
places. Pydantic generally copies model defaults, but the convention is fragile and easy to break
when values are reused outside model construction.

Fix:

- Use Field(default_factory=dict) consistently.
- Add a test that mutating one loaded config cannot mutate another.

## A-10 — Database portability is asserted more strongly than tested (Medium)

Storage is designed for SQLite and PostgreSQL, but the migration and concurrency evidence reviewed
here is primarily SQLite. Dialect-sensitive behavior includes concurrent claim/update semantics,
tuple comparisons, JSON equality, full-text search, advisory locking, and transaction isolation.

Fix:

- Run migrations and the state-machine/ingestion concurrency suite against PostgreSQL in CI.
- Keep dialect-specific code isolated in the already-justified search boundary and migration helpers.
- Test restart recovery and concurrent claims on both engines.

## A-11 — Ambient wall-clock reads undermine deterministic behavior (Low/Medium)

API routes, image cache, retention, and session handling use datetime.now directly while the test
guidance says Clock is injected. See [deps.py:203](../../aggregato/api/deps.py:203) and
[runs.py:129](../../aggregato/api/routes/runs.py:129).

Fix:

- Inject one application Clock into routes/services.
- Preserve monotonic clocks only for network pacing and subprocess deadlines.
- Add exact boundary tests for expiry, retention, and retry decisions.
