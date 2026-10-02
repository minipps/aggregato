# Architecture

Aggregato is a single-user service with a FastAPI API, a separate scheduler worker, one isolated child process per provider run, and a SQLite or PostgreSQL database. The API serves the Vue application and queues work; only the worker runs providers.

Pending development work is tracked in [roadmap.md](roadmap.md). The HTTP and provider contracts live in [contracts/openapi.yaml](contracts/openapi.yaml) and [contracts/provider-plugin.md](contracts/provider-plugin.md); provider authoring and acquisition policy are in [writing-a-provider.md](writing-a-provider.md) and [CONTRIBUTING.md](../CONTRIBUTING.md).

## Runtime and trust boundaries

| Component | Responsibility |
|---|---|
| API ([main.py](../aggregato/main.py)) | Serves /api/v1 and the built SPA, authenticates requests, and queues work. It never runs a sync. |
| Worker ([worker.py](../aggregato/worker.py)) | Owns the durable due queue, claims providers and queued jobs, dispatches runs, and schedules retries. Run one worker per database; a process-lifetime database lock prevents a second owner. |
| Sync child ([runner.py](../aggregato/sync/runner.py), [child.py](../aggregato/sync/child.py)) | A fresh child imports one provider for a run and sends JSON-lines messages to its parent. It has no database engine; the parent validates messages and performs writes. |

The child process and wall-clock timeout contain ordinary crashes and hangs, but are not an OS sandbox. Provider code still has the service account's filesystem and network permissions. The provider contract requires the host-owned HTTP client for rate limits and request policy. An unreviewed Python drop-in must be treated as executable code.

Storage uses SQLAlchemy Core and explicit connections, without an ORM or repository layer. SQLite with WAL is the default; PostgreSQL uses the same application schema. Full-text search has a SQLite FTS5 implementation and a PostgreSQL tsvector implementation.

Import boundaries are checked by import-linter: API and sync code depend inward through ingest, db, and domain. Import-linter forbids provider imports of db and ingest, or imports of httpx2 outside the host client. See [pyproject.toml](../pyproject.toml) and [providers/http.py](../aggregato/providers/http.py).

## Sync and ingest invariants

- Provider discovery is static and side-effect free. A fresh install makes no outbound request.
- normalize is pure. Bump a provider's schema_version when retained payloads need a different mapping; the worker replays stale payloads in bounded batches before fetching. An incomplete replay blocks the fetch.
- The parent validates each record. A rejected record rolls back to its savepoint, becomes an ingest_failures row, and does not stop later records.
- Upserts and unique constraints provide idempotency. Events without a platform event id use the fallback key (provider item, kind, logged time, canonical subject reference).
- The cursor advances only through a checkpoint the child flushed. A partial run keeps accepted records and resumes from the last committed checkpoint.
- Deletes are tombstones. Inferred deletion is allowed only after a successful full run, with operator opt-in, for a provider that does not report deletes, after the window sanity check, and with no record failures.

The provider contract also requires closed vocabularies and preserves platform identifiers and raw role names. Structural changes must fail the run rather than report a misleading success. [CONTRIBUTING.md](../CONTRIBUTING.md) covers acquisition policy, including the ban on CAPTCHA or anti-bot circumvention.

## Performance targets

These are targets, not capacity claims for every deployment. The bench suite covers only the checks described by its tests; its local query timing sample does not establish API latency at one million rows.

| Workload | Target |
|---|---|
| Filtered entries, first page at 1M rows | p95 under 1,000 ms |
| Filtered entries, deep page at 1M rows | Within 20% of first-page latency |
| Ingest scaling, 10k to 1M rows | No more than 10% throughput regression |
| Batched creator resolution | At least 20,000 credit lookups/minute |
| Clean install to first browsable sync | Under 15 minutes wall time; under 5 minutes operator time |
| Successful now-playing check | Every 15 seconds |
| Now-playing WebSocket refresh | Poll durable state every 500 ms; normal visibility within 16 seconds |
| Now-playing freshness | Omit items last checked more than 45 seconds ago |
