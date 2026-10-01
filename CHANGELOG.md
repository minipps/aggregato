# Changelog

## Unreleased

- Restrict portable archives and operational diagnostics to operators. SQLite archives omit
  configured credentials, sessions, queued jobs, failure payloads, diagnostic logs, and cursor
  state. Retained personal history and provider payloads still make archives private.
- Retain only HTTP method, sanitized endpoint URL, and status in response diagnostics. Public and
  read-only run summaries hide logs, error detail, and cursors.
- Preserve valid archive facts when normalization replay rejects a record. Replay runs in bounded
  batches and resumes from committed replacements after interruption. Failed full runs cannot
  infer deletions.
- Keep manual creator splits during resync and merge/undo operations. Forward migrations add
  historical split provenance and an index for provider-item entry lookups.
- Treat a missing next-run timestamp as unscheduled. Explicit enable, sync, and valid configuration
  changes can reactivate a suspended history sync; checks and queued imports/replays remain
  available while disabled.
- Enforce one scheduler worker per database before startup recovery. Shutdown drains briefly,
  cancels remaining work, and awaits provider-child cleanup. Both container processes use asyncio.
- Support WebSocket upgrades through the production proxy, release idle handlers on disconnect,
  and contain backend static-file paths.
- Omit impossible NULL branches from non-null keyset sorts, so deep pages seek their timestamp
  range instead of scanning preceding rows. Settings shows when instance configuration locks image
  caching and preserves the database preference while that gate is disabled.
- Run title and review search in the database before pagination. Weekly statistics consistently
  use ISO weeks. Settings reports rendered-JSON payload estimates and the configured SQLite file
  size; PostgreSQL backups use native database tools and a separate image-cache copy.
- Fix stale frontend responses, pending/error feedback for corrections, structured undo state,
  provider-check polling, native form submission, nullable settings, and session-aware live updates.
- Make migration backups only when upgrades are pending. Remove duplicate dispatch records,
  repeated settings validation, full creator-memo copies, and unused scheduler jitter.

Bundled providers remain Koito, ListenBrainz, AniList, Letterboxd, Spotify, and Goodreads. Feed and
recently-played sources expose limited windows; a full resync cannot recover history the source no
longer supplies. See the [provider table](README.md#platforms) and [operations guide](docs/operations.md)
for acquisition and backup limits.
