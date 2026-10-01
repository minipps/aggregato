# Research notes

Reviewed 2026-09-30. This page records the current implementation choices and their costs.

## Scheduling

A dedicated worker polls a database-backed due queue. The database stores next-run times, retry
state, and run lineage, so the default deployment needs no message broker. A non-blocking OS `flock`
on a SQLite database sidecar or a PostgreSQL session advisory lock enforces one worker per database.
The worker acquires the lock before migrations and interrupted-run recovery. This is process-lifetime
single-owner enforcement, not a durable multi-host lease; losing the PostgreSQL session releases its
lock. Shutdown stops admission, gives active syncs three seconds to drain, then cancels and awaits
remaining run tasks so their children are reaped. The now-playing monitor stops concurrently, and
the engine is disposed last. The project owns admission, recovery, and shutdown behavior.

## Provider process boundary

Each provider operation runs in a short-lived child process. The child imports the selected provider,
receives only that provider's configuration, and has no database engine. The parent validates output
and performs writes. A timeout and process boundary limit ordinary crashes and hangs; they do not
sandbox Python code. Drop-ins retain the service's filesystem and network permissions, so operators
must review their source before enabling them.

## Storage and migrations

SQLAlchemy Core is used over SQLite (WAL) by default and PostgreSQL as an option. Alembic migrations
are forward-only. Shared types and explicit dialect-specific statements keep both databases
supported without an ORM or an additional service. SQLite backups use its backup API so committed
WAL content is included.

The Settings archive is available for on-disk SQLite only. It contains a consistent database
snapshot, cached images, and public configuration; configured secrets, sessions, ingest-failure
payloads, run diagnostics, and queued jobs are omitted. A restored database has providers disabled
until the operator enters credentials. PostgreSQL backup and restore use PostgreSQL tools, with the
image cache backed up separately. This archive remains private because it includes personal history
and retained provider-item payloads; it does not promise to remove secrets from arbitrary payloads.

## Provider requests

Providers use the host-owned `ctx.http` client. It applies acquisition-mode rate floors, serializes
scraper requests per host, and honors `Retry-After`. Bundled code is checked by conformance tests and
import rules. The Python process boundary cannot prevent arbitrary unreviewed code from opening a
separate network client, so drop-ins remain a source-review responsibility. CAPTCHA or anti-bot
circumvention is prohibited; a provider reports a block and stops.

## Normalization replay

`normalize` is pure so stored provider payloads can be reprocessed without a network request or a
clock dependency. A provider's `schema_version` identifies the mapping used for stored payloads.
Replay reads bounded batches, limits each child request by record count and bytes, and commits each
replacement batch atomically. The stored version advances only after replay completes; a failed or
oversized record remains stale for operator follow-up.

## Identity and ratings

Platform identifiers take precedence over unambiguous name matching. Ambiguities stay in the
resolution queue. Operator choices are durable, and creator splits are recorded against individual
credits so later syncs preserve the split. Merge and split actions are reversible.

Ratings retain their source value and scale. Normalized values are derived from linear formulas or
explicit ordinal maps and are meaningful only within their source scale. Logged dates also retain the
precision supplied by the platform.

## Search and pagination

Search uses SQLite FTS5 or PostgreSQL `tsvector` behind one query interface. Collection pagination is
keyset-based and includes a stable ID tiebreaker; offsets are not used. These choices avoid requiring
a separate search service and keep page order deterministic. Performance budgets remain targets
until measurements at their stated archive sizes are recorded.

## Current playback

`now_playing` is an optional provider capability. The worker polls it through the same child boundary
and stores at most one parent-validated item per provider. The API serves a fresh snapshot over an
authenticated WebSocket. Playback remains transient source-specific state and does not enter history
or identity resolution.

## API and frontend

The API uses bearer tokens or a database-backed browser session; cookie-authenticated writes use
CSRF protection. A read-only token is method-gated on the server. Public GET/HEAD access is an
explicit setting, while exports and raw diagnostics require operator authentication.
Run diagnostics expose only the HTTP method, sanitized URL, and status code; response bodies and
headers are not captured in the diagnostic response.

The frontend is a Vue single-page application served by Nginx. Docker images include built assets;
building the frontend from source requires Node. The browser consumes the documented API, and the
server enforces authentication and authorization. Frontend contract types are handwritten against
the OpenAPI document.

## Testing and measurement

Tests use recorded fixtures, block sockets, and inject time so provider behavior and retries are
reproducible. The conformance suite checks bundled providers. Benchmarks cover selected query and
ingest paths, but do not currently prove every declared latency and throughput target at the stated
scale. Treat those figures as targets until representative measurements are recorded.
