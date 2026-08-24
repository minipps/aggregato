# Now-playing provider WebSocket plan

## Outcome

Add an authenticated `/api/v1/ws/now-playing` endpoint that sends a complete initial snapshot and
then only changed snapshots. The worker polls enabled, capable providers every 15 seconds through
the existing isolated child process, stores one normalized transient item per provider, and the API
reads that durable state.

The first bundled implementations are Koito and ListenBrainz. Results remain source-specific: if
both report playback, the snapshot contains two items. Idle, failed, disabled, unsupported, and
stale providers are omitted. No playback history is retained.

Performance target: under normal operation, a provider change is emitted within 16 seconds. An
item is stale and omitted after 45 seconds (three missed polls).

## Public contracts

### Provider contract

- Add `Capability.NOW_PLAYING = "now_playing"`.
- Add an optional runtime-checkable `NowPlayingProvider` protocol with
  `async def now_playing(ctx: ProviderContext) -> NowPlayingItem | None`.
- `NowPlayingItem` contains `work: NormalizedWork`, normalized credits, work external identifiers,
  and creator external identifiers. It contains no logged entry, opinion, playback progress,
  duration, or raw provider payload.
- A provider opts in through its static manifest and runtime capability declaration. Providers
  without the capability remain valid and unchanged. This is additive, so provider API version 1
  remains current.

### WebSocket contract

`/api/v1/ws/now-playing` sends:

```json
{
  "type": "snapshot",
  "generated_at": "2026-08-24T12:00:00Z",
  "items": [
    {
      "provider_id": "listenbrainz",
      "changed_at": "2026-08-24T11:59:52Z",
      "work": {
        "media_type": "track",
        "title": "Track title",
        "original_title": null,
        "release_year": null,
        "sequence_number": null,
        "image": "/api/v1/media/image/<sha256>",
        "metadata": {"release_name": "Release title"}
      },
      "credits": [],
      "external_ids": [],
      "creator_external_ids": []
    }
  ]
}
```

- Items are ordered by `provider_id`.
- `changed_at` is the host time when that provider's normalized item last changed; successful
  refreshes of the same item do not alter it.
- Artwork uses the existing local image-cache path and never exposes a third-party redirect.
- Authentication matches `/api/v1/ws/sync`: bearer or session cookie, no query-string credential,
  read-only credentials accepted, and unauthenticated handshakes closed with code `1008`.
- The socket accepts no client messages. Reconnects receive the complete current snapshot.
- There is no HTTP fallback endpoint and no SPA work in this change.

## Durable state and failure policy

Extend `provider_state` with:

| Column | Type | Purpose |
|---|---|---|
| `now_playing_item` | portable JSON, nullable | Latest parent-validated normalized item |
| `now_playing_changed_at` | timestamp, nullable | When the semantic item last changed |
| `now_playing_checked_at` | timestamp, nullable | Last completed attempt, used for 45-second expiry |
| `now_playing_next_poll_at` | timestamp, nullable | Independent now-playing schedule; `NULL` suspends polling |
| `now_playing_failures` | integer, non-null, default `0` | Position in the transient retry ladder |
| `now_playing_config_fingerprint` | string(64), nullable | SHA-256 of resolved settings plus provider schema version |

Rules:

- Success with an item stores it, resets failures, and schedules `now + 15 seconds`.
- Success with no playback clears the item, resets failures, and schedules the same interval.
- An unchanged item refreshes `checked_at` and the next poll only, preventing duplicate socket
  messages.
- Any failed attempt clears the active item immediately; clients therefore never receive a known
  stale item as current.
- Transient failures use the existing 1m/5m/15m/1h ladder, capped at 1h after exhaustion, and
  honour a longer provider `Retry-After`.
- Auth, blocked, and structure-change failures set `next_poll_at` to `NULL`. They resume only after
  settings, provider schema, or enablement changes; only a fingerprint is stored, never secrets.
- Disabling a provider clears its current item and schedule. Enabling or saving configuration makes
  a capable provider due immediately.
- The API additionally omits rows whose `checked_at` is older than 45 seconds, so an API-only or
  worker-wide failure cannot leave a permanent false presence.

## Atomic implementation tasks

Each task owns its listed production files and its focused tests. Agents should not edit files
owned by another active task. Tasks 1 and 2 can start immediately; Tasks 3 and 4 can run in parallel
after Task 1; Tasks 6 and 7 can run in parallel after Tasks 1 and 2.

### Task 1 — Core provider and child-process contract

**Depends on:** nothing  
**Unblocks:** 3, 4, 5, 6, 7

Owned areas: domain models/enums, provider base contract and bundled static manifest declarations,
child JSON-lines protocol, child entrypoint, and runner.

- Add the capability, `NowPlayingItem`, optional provider protocol, and Koito/ListenBrainz manifest
  declarations.
- Add `operation: "sync" | "now_playing"` to `RunRequest`, defaulting to `sync`; do not add a
  `FetchMode`, because presence is not an ingest mode.
- Add one protocol message wrapping a nullable now-playing result so “no playback” is distinct from
  “the child emitted no result.”
- In the child, validate the declared capability and optional protocol, call `now_playing` inside
  the existing polite HTTP client, and emit exactly one result.
- In the parent, validate that a now-playing operation emits only that result or one classified
  error. Reuse all current line/byte/message caps, stderr capture, process-group termination, and
  wall-clock handling.
- Add protocol round-trip and runner-supervision tests covering active, empty, malformed,
  duplicate, unexpected-message, crash, and timeout results.

**Done when:** a fixture provider can return a validated item or `None` through the existing child
boundary without ingesting records or creating a `sync_runs` row.

### Task 2 — Schema and forward-only migration

**Depends on:** nothing  
**Unblocks:** 6, 7

Owned areas: SQLAlchemy schema, migration `0018`, and migration/schema portability tests.

- Add the six `provider_state` columns above using portable types and named/default constraints
  consistent with the existing schema.
- Add a forward-only migration from revision `0017`; do not edit earlier revisions.
- Seed a complete pre-0018 provider row in a migration test, upgrade, and assert all prior values
  survive and the new fields have safe empty defaults on SQLite. Keep the schema portable to
  Postgres.

**Done when:** old databases upgrade without data loss and fresh metadata matches migrated metadata.

### Task 3 — ListenBrainz implementation

**Depends on:** Task 1  
**Can run with:** Task 4

Owned areas: ListenBrainz provider, ListenBrainz unit tests, and
`tests/fixtures/listenbrainz/now-playing-*.json`.

- Call `GET {base_url}/1/user/{quoted_username}/playing-now` with the existing optional token
  header and status classification.
- Require the documented `payload.playing_now` boolean and a list containing zero or one listen;
  do not require `listened_at`, which the playing-now response intentionally omits.
- Reuse/refactor the existing track, artist, release metadata, and identifier normalization so
  historical and current listens cannot drift.
- Return `None` for a valid inactive response and raise `StructureChangedError` for malformed
  envelopes or multiple current listens.
- Test exact URL construction, active/idle responses, authentication/rate-limit errors, malformed
  structure, identifiers, and normalization parity with an equivalent stored listen.

**Done when:** recorded active and idle responses produce deterministic results without network
access or a clock read in provider code.

### Task 4 — Koito implementation

**Depends on:** Task 1  
**Can run with:** Task 3

Owned areas: Koito provider, Koito unit tests, and `tests/fixtures/koito/now-playing-*.json`.

- Call `GET {base_url}/apis/web/v1/now-playing` with the existing Koito API-key header and status
  classification.
- Parse `currently_playing`; require a valid `track` only when it is true.
- Reuse/refactor existing track, artist, Koito/MusicBrainz identifier, and relative-artwork
  normalization. Preserve the configured origin when resolving artwork.
- Return `None` when `currently_playing` is false and raise `StructureChangedError` for malformed
  active responses.
- Test exact URL/header behavior, active/idle responses, authentication/rate-limit errors,
  malformed structure, identifiers, and normalization parity with an equivalent stored listen.

**Done when:** recorded active and idle responses produce deterministic results without network
access or a clock read in provider code.

### Task 5 — Provider conformance extension

**Depends on:** Tasks 1, 3, and 4

Owned areas: shared provider conformance fixtures/helpers and conformance tests only.

- For every provider declaring `now_playing`, require the optional protocol method and recorded
  active and idle cases.
- Assert deterministic normalized output, closed vocabularies, complete identifiers, no direct
  HTTP client construction, and correct capability/manifest parity.
- Do not require now-playing fixtures or methods from providers that do not opt in.

**Done when:** both bundled implementations pass the shared contract and all existing providers
remain unaffected.

### Task 6 — Worker monitor and provider lifecycle

**Depends on:** Tasks 1 and 2  
**Can run with:** Task 7

Owned areas: new `aggregato/sync/now_playing.py`, worker wiring, reuse of dispatch configuration
helpers, provider enable/configuration lifecycle updates, and monitor unit tests.

- Reuse or minimally extract the current logic that reads provider settings, separates secrets,
  and builds an isolated child request; do not duplicate secret handling.
- Add a worker-side monitor with injected clock and a separately testable `poll_once`. Limit
  concurrent presence children to three and give each a 30-second wall clock.
- Discover capabilities only from static manifests. Poll only enabled, correctly configured
  providers and never pass a database engine to a child.
- Implement the success, clearing, retry, suspension, fingerprint, and image-source-registration
  transitions defined above in short transactions. Preserve `changed_at` for equal normalized
  payloads.
- Run the monitor beside the scheduler and retention task. On shutdown, cancel it and rely on the
  existing runner cleanup to terminate any child process.
- Reset or clear now-playing state atomically with provider configuration and enable/disable
  writes.
- Test selection, concurrency cap, exact scheduling with a frozen clock, identical-item
  suppression, idle/error clearing, retry ladder, `Retry-After`, permanent suspension,
  fingerprint/config reactivation, image registration, disable behavior, and cancellation. Tests
  must not sleep or open sockets.

**Done when:** the database contains at most one fresh, parent-validated item per capable provider,
with no interaction with normal sync scheduling or history.

### Task 7 — Authenticated WebSocket API

**Depends on:** Tasks 1 and 2  
**Can run with:** Task 6

Owned areas: new now-playing API route, API response models/image-path reuse, app router wiring,
OpenAPI component schemas/descriptions, and WebSocket contract tests.

- Add the exact snapshot models above. Convert internal `image_url` to the existing local image
  path by promoting/reusing the current API image-path helper rather than duplicating hashing.
- Query capable provider ids from static discovery and active rows from durable state. Filter by
  enabled state and the 45-second threshold, validate stored JSON, and sort by provider id.
- Reuse the `/ws/sync` authentication behavior. Send an initial snapshot, then poll durable state
  every 500 ms and send only when the snapshot excluding `generated_at` changes.
- Add the route to the app and describe the non-OpenAPI WebSocket path plus its component schemas
  in `docs/contracts/openapi.yaml`; do not add an HTTP route.
- Contract-test bearer and cookie authentication, read-only credentials, close `1008`, initial
  active and empty snapshots, deterministic ordering, change/clear/stale emissions, unchanged
  suppression, and reconnect snapshots.

**Done when:** a client can connect after either process restarts and immediately receive the
current non-stale items without the API importing `aggregato.sync`.

### Task 8 — Canonical documentation and performance declaration

**Depends on:** Tasks 1 and 2; reconcile final names after Tasks 6 and 7

Owned areas: requirements, architecture, research rationale where needed, data model, provider
plugin contract, provider-writing guide, and validation guide. Do not edit the OpenAPI file owned
by Task 7.

- Add acceptance scenarios for active, idle, duplicated cross-provider, failed, disabled, and
  worker-stale states.
- Document the new provider capability/method, transient storage fields and transitions, worker/API
  boundary, retry behavior, and offline conformance expectations.
- Declare the 15-second acquisition plus 500-ms socket polling budget (maximum 16 seconds) and
  45-second stale cutoff. Add a deterministic timing assertion using injected clocks rather than a
  wall-clock benchmark.
- State the intentional limits: no HTTP fallback, frontend panel, playback progress, history,
  cross-provider unification, event broker, or persistent provider worker.

**Done when:** canonical docs and implementation describe the same contract and state transitions.

### Task 9 — Integration and merge gate

**Depends on:** Tasks 1–8

Owned areas: integration test additions and conflict resolution only; production changes go back to
the owning task.

- Exercise worker child result → durable state → authenticated WebSocket snapshot end to end using
  recorded providers and injected clocks.
- Assert simultaneous Koito and ListenBrainz playback yields two source-specific items; idle or
  stale results disappear and create no entries, provider items, or sync runs.
- Run and fix the complete gates:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
uv run pytest -m "not bench"
uv run pytest tests/conformance
uv run pytest -m bench
cd frontend && npm run type-check && npm run test:unit
```

**Done when:** every gate is clean with warnings treated as errors and benchmark baselines have not
regressed by more than 10%.

## Parallel execution map

```text
Task 1 ─┬─> Task 3 ─┐
        ├─> Task 4 ─┼─> Task 5 ─┐
        ├───────────┼─> Task 6 ─┤
        └───────────┼─> Task 7 ─┼─> Task 9
Task 2 ─────────────┼─> Task 6 ─┤
        └───────────┼─> Task 7 ─┤
                    └─> Task 8 ─┘
```

## Assumptions and deliberate omissions

- ListenBrainz uses its documented `GET /1/user/{username}/playing-now` response, which omits
  `listened_at` for the current listen.
- Koito uses the authenticated `GET /apis/web/v1/now-playing` route and
  `currently_playing`/`track` response from tagged v0.3.2.
- The 15-second interval is a host constant for this first implementation. Add a provider-declared
  or operator setting only after a real platform limit requires it.
- Short-lived children are reused despite process churn because they already provide isolation and
  supervision. Consider a persistent per-provider child only if measurement shows the fixed
  15-second cadence materially harms supported hardware.
- Cross-provider deduplication is intentionally absent: source-specific truth is safer than title
  heuristics, and transient items do not pass through stored work identity resolution.

References:

- [ListenBrainz playing-now API](https://listenbrainz.readthedocs.io/en/latest/users/api/core.html)
- [Koito v0.3.2 routes](https://github.com/gabehf/koito/blob/v0.3.2/engine/routes.go)
- [Koito v0.3.2 now-playing handler](https://github.com/gabehf/koito/blob/v0.3.2/engine/handlers/now_playing.go)
