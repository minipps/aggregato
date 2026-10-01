# Changelog

## [1.0.0](https://github.com/minipps/aggregato/releases/tag/v1.0.0) — 2026-10-01 UTC

- Add a Spotify recently-played provider and a Media view for browsing works.
- Restrict archives and operational diagnostics to operators. Read-only run summaries hide logs,
  detailed errors, and cursors; response diagnostics retain only the method, sanitized endpoint,
  and status.
- Preserve valid data when normalization replay encounters a bad record or is interrupted, and
  retain manual creator splits through resync and merge/undo operations. Failed full runs cannot
  infer deletions.
- Enforce one scheduler worker per database and bound shutdown while provider children are cleaned
  up. Suspended history syncs resume only after an explicit enable, sync, or valid configuration
  change; checks and queued imports or replays remain available while disabled.
- Apply title and review search before pagination, preserve indexed keyset bounds for deep pages,
  and calculate weekly statistics by ISO week.
- Improve correction feedback, undo state, provider-check polling, settings, and session-aware live
  updates. Show when instance configuration locks image caching while preserving its saved
  preference, and report payload estimates and SQLite file size.
- Support WebSocket upgrades through the production proxy, release idle handlers on disconnect,
  contain backend static-file paths, and include the PostgreSQL driver in the backend image.
- License the project under AGPLv3 and add private vulnerability reporting; update provider,
  operations, and release guidance and remove stale specifications.

### Upgrade and data handling

The API applies pending migrations automatically at startup. This release adds forward-only
migrations [0019](aggregato/db/migrations/versions/0019_durable_creator_splits.py), which preserves
provenance for existing manual creator splits, and [0020](aggregato/db/migrations/versions/0020_entry_provider_item_index.py),
which indexes entry lookups by provider item. SQLite creates a pre-migration backup when an upgrade
is pending; PostgreSQL does not, so take a native backup before upgrading.

The portable archive is available only for on-disk SQLite. It omits configured credentials, sessions, queued
jobs, failure payloads, diagnostics, and cursor state, but still contains personal history and
retained provider payloads. Keep archives private. Full database backups can also contain
credentials and sessions and should remain private. Feed and recently-played sources expose limited
windows; a full resync cannot recover history the source no longer supplies. See the
[provider table](README.md#platforms) and [operations guide](docs/operations.md) for details.

## [0.2.5](https://github.com/minipps/aggregato/releases/tag/v0.2.5) — 2026-08-24 UTC

- Add provider now-playing updates over WebSockets.
- Add public read-only frontend access and improve sync history diagnostics.

## [0.2.4](https://github.com/minipps/aggregato/releases/tag/v0.2.4) — 2026-08-18 UTC

- Expose live sync progress and WebSocket status.

## [0.2.3](https://github.com/minipps/aggregato/releases/tag/v0.2.3) — 2026-08-10 UTC

- Refactor sync and provider surfaces.

## [0.2.2](https://github.com/minipps/aggregato/releases/tag/v0.2.2) — 2026-08-08 UTC

- Simplify image-cache validation.

## [0.2.1](https://github.com/minipps/aggregato/releases/tag/v0.2.1) — 2026-08-08 UTC

- Run the frontend in the production Docker stack and make the release script transactional.

## [0.2.0](https://github.com/minipps/aggregato/releases/tag/v0.2.0) — 2026-08-07 UTC

- Fix SQLite scheduler claim admission and retire unsupported podcast media types.
- Complete the adversarial review remediation backlog and update release examples automatically.

## [0.1.5](https://github.com/minipps/aggregato/releases/tag/v0.1.5) — 2026-08-04 UTC

- Update the frontend toolchain, fix blank provider settings and creator identifier resolution, and
  replace the over-time statistic with a daily activity heatmap.

## [0.1.4](https://github.com/minipps/aggregato/releases/tag/v0.1.4) — 2026-08-04 UTC

- Add the Bento-style Catppuccin Mocha interface, fix linked resolution decisions, and hide the
  fixture provider from API listings.

## [0.1.3](https://github.com/minipps/aggregato/releases/tag/v0.1.3) — 2026-08-04 UTC

- Serialize SQLite sync runs after restart; refresh Letterboxd RSS and infer its username from
  imports.
- Show safe provider settings and sync Goodreads bookshelf RSS.

## [0.1.2](https://github.com/minipps/aggregato/releases/tag/v0.1.2) — 2026-07-30 UTC

- Rename the `tv_series` and `anime_series` media types to `tv` and `anime`, and add a completed
  status filter for entries.
- Rate AniList using the account's score format and support configured cross-origin preflights.
- Forward optional API tokens and CORS origins through Compose, pass run payloads to the child on
  stdin, and serve cached covers without a credential.

## [0.1.1](https://github.com/minipps/aggregato/releases/tag/v0.1.1) — 2026-07-30 UTC

- Merge seasons into series, backfill artwork onto existing works, and use AniList's list status.
- Carry full-resync requests through to providers and add a release script matching tag-workflow
  checks.

## [0.1.0](https://github.com/minipps/aggregato/releases/tag/v0.1.0) — 2026-07-30 UTC

- Initial release; see its [commit history](https://github.com/minipps/aggregato/commits/v0.1.0).
