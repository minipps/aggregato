# Contract: Provider Plugin

**Provider API version**: 1.0

This is the public contract for provider packages. Contract changes follow semantic versioning;
breaking changes require a migration note and an updated conformance suite.

Bundled providers are described by host-owned static manifests. Drop-in providers supply a
`manifest.json`; discovery reads this metadata without importing provider code or making network
requests and labels drop-ins unreviewed. The worker imports a provider only when it is selected.

---

## 1. The interface

```python
class Provider(Protocol):
    id: str                          # stable slug, e.g. "letterboxd"; keep it stable across updates
    name: str                        # display name
    media_types: set[MediaType]      # from the closed enum; a provider cannot invent one
    capabilities: set[Capability]
    acquisition: Acquisition         # api | feed | export | scrape
    config_model: type[BaseModel]    # runtime configuration validation
    rating_scales: list[RatingScale]
    schema_version: int              # bump to trigger normalization replay
    default_poll_interval: timedelta # from the platform's own rate limits, not a global default

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]: ...

    def normalize(self, raw: RawRecord) -> NormalizedBatch: ...

    async def check(self, ctx: ProviderContext) -> CheckResult: ...
```

`config_model` is the Pydantic runtime validator. The settings form renders `config_schema` from the
host-owned bundled manifest or the drop-in's `manifest.json`; bundled manifest schemas are checked
for parity with the provider's runtime model schema.

`fetch` handles `incremental`, `full`, and `import` modes. In `import` mode, `ctx.import_path` is set
and the provider must not make network requests. `check` calls `provider.check`; replay passes stored
records directly to `normalize` without calling `fetch`.

`fetch` may yield a `Checkpoint` between records. The host persists the enclosed cursor, and a
partial run resumes from its last flushed checkpoint. A provider that cannot resume from a cursor
may start each fetch from the beginning; host upserts keep already-ingested records idempotent.

Bump `schema_version` whenever `normalize` changes how retained payloads map to rows. Before the
next fetch, the host replays stale payloads in bounded batches and commits each accepted record
atomically: a rejected normalization or write keeps the previous derived rows and item version. An
incomplete replay is recorded as non-success, retries the remaining stale items on a later run, and
does not proceed to fetch. Full-run deletion inference is also skipped when any record fails
normalization or ingest.

The optional `Capability.NOW_PLAYING` capability adds a runtime-checkable protocol:

```python
class NowPlayingProvider(Protocol):
    async def now_playing(self, ctx: ProviderContext) -> NowPlayingItem | None: ...
```

The method returns one normalized current work or `None` for a valid idle response. A provider must
declare `now_playing` in both its static manifest and runtime capability set before the host will
call it; providers without the capability remain valid and unchanged. This is presence, not a
history fetch mode, and the result contains no playback progress, duration, logged event, or raw
payload.

This additive capability does not change provider API version 1.0; existing providers need no method
or fixture change unless they opt in.

### Hard rules

| Rule | Why | How it is enforced |
|---|---|---|
| `normalize` is **pure**: no network, clock, randomness, or storage | Replay must produce the same derived rows from retained payloads | Conformance calls it twice for each fixture record; sockets are blocked, the clock is frozen, and writes are checked |
| A provider receives only its selected context and no database engine | Database writes and validation remain in the host process | The worker child passes explicit inputs without an engine; import-linter forbids provider modules from importing `aggregato.db` or `aggregato.ingest`. This process boundary is not an OS sandbox. |
| A provider uses the host HTTP client | The host enforces request pacing, acquisition floors, and contact identity | Use `ctx.http`; import-linter forbids provider modules from importing `httpx` |
| A provider uses the closed `media_type`, `role`, and `subject_ref` vocabularies | Queries and stored rows share one core vocabulary | Parent-side ingest validation rejects invalid records into `ingest_failures` |
| Extract every identifier present in a payload | Aggregato does not enrich records from third-party metadata sources | Conformance checks that fixture identifiers appear in normalized output |
| Never bypass a CAPTCHA or anti-bot challenge | Acquisition policy forbids circumvention | Raise `BlockedError`; the host stops and does not retry automatically |
| `now_playing` returns only a normalized item or `None` | Current playback is transient, not history | The child validates one `NowPlayingItem` or `None`; the host stores the current result outside ingest |

---

## 2. What the host provides

`ProviderContext`:

| Field | Guarantee |
|---|---|
| `http` | `httpx.AsyncClient` wrapped with rate limiting at `max(declared, host_floor)`, one in-flight request per host within that run for `scrapes` providers, retry on 5xx/429/transport with jitter, `Retry-After` compliance, ETag pass-through, and the project User-Agent with a contact URL |
| `config` | This provider's resolved settings, validated by its `config_model` |
| `secrets` | This provider's credentials, resolved before the child starts and sent explicitly |
| `log` | Logger already bound to `provider_id`, `run_id`, `lineage_id` |
| `state` | Per-run scratch mapping. Changes are not returned by the child protocol or persisted; yield a `Checkpoint` for durable cursor progress. |
| `import_path` | Set only in `import` mode |

The host owns scheduling, the retry ladder, rate limiting, cursor persistence, idempotency, identity
resolution, storage, migrations, image caching, and the 15-second now-playing poll schedule.

---

## 3. What a provider returns

`NormalizedBatch` — Pydantic, serialized as one JSON line across the process boundary:

```python
class NormalizedBatch(BaseModel):
    work: NormalizedWork                       # required: the item this record concerns
    entries: list[NormalizedEntry] = []
    opinions: list[NormalizedOpinion] = []
    credits: list[NormalizedCredit] = []       # optional per provider
    external_ids: list[NormalizedExternalId] = []
    creator_external_ids: list[NormalizedCreatorId] = []
```

Rules that catch real mistakes:

- `logged_precision` is **required** on every entry and has no default; do not infer a precision the
  source did not provide.
- `rating_raw` requires a `rating_scale_id` the provider declared in `rating_scales`.
- `subject_ref` uses only `season`, `episode`, `track`, `disc`, `chapter`, `volume`, positive integers.
- `position` on credits is the platform's order, or payload order when billing order is absent.
- `role_raw` carries the platform's own word verbatim, always, even when `role` maps cleanly.
- A provider that finds nothing must return an empty iterator. It must **not** report success on a
  structural failure — raise `StructureChangedError` instead, because silence plus delete inference is
  how an archive gets erased.

---

### Optional current-playback result

`NowPlayingItem` is the normalized, transient counterpart to `NormalizedBatch`:

```python
class NowPlayingItem(BaseModel):
    work: NormalizedWork
    credits: list[NormalizedCredit] = []
    external_ids: list[NormalizedExternalId] = []
    creator_external_ids: list[NormalizedCreatorId] = []
```

The host stores at most one item per provider. An equal item refreshes its checked time without
changing its semantic change time; an idle result or any failed attempt clears it. Artwork remains a
provider-supplied source URL at this boundary and is served through the host's local image cache at
the API edge. Providers do not construct a second client, read playback history, or emit progress.

## 4. Error signalling

Raise provider errors from `fetch` or `now_playing`; the host classifies them and decides retry
policy. In `normalize`, raise `ValueError` for an invalid record or `StructureChangedError` when the
source structure has changed. `check` returns a `CheckResult` for both success and failure.

| Exception | `error_class` | Host behaviour |
|---|---|---|
| `AuthError` | `auth` | No automatic retry; the provider becomes degraded until an operator acts. |
| `BlockedError` | `blocked` | No automatic retry; the provider becomes degraded. Do not retry or circumvent a block. |
| `StructureChangedError` | `structure_changed` | No automatic retry; the provider becomes degraded until its implementation is updated. |
| `RateLimited(retry_after=…)` | `rate_limit` | Retries, honors `Retry-After`, and lengthens the effective interval for the rest of the session. |
| `TransportError`, `httpx` transport errors, or a raised 5xx response | `transport` | The HTTP client retries in-run; remaining failures use the run retry policy. |
| Validation failure on one record | `parse` | The record goes to `ingest_failures`; the run continues. |
| Anything else | `internal` | Uses the run retry policy; the traceback is retained in the run log excerpt. |

The same classifications apply to `now_playing`. A transient failure clears the stored item and
uses the host retry ladder; `auth`, `blocked`, and `structure_changed` suspend the independent
presence schedule until configuration, provider schema, or enablement changes.

---

## 5. Conformance suite — a merge gate

Every bundled provider must be registered in `tests/conformance/` and pass the suite. Its fixtures
are recorded inputs; the tests run offline without provider credentials.

The suite checks stable IDs, valid schema versions and poll intervals; emitted media types are
declared; and providers claiming `has_ratings`, `has_reviews`, or `has_credits` produce matching
fixture output. It exercises file imports, requires `Capability.SCRAPES` to match
`Acquisition.SCRAPE`, checks deterministic normalization and closed vocabularies, and checks that
recognized fixture identifiers appear in normalized output. It checks cursor resume when a provider
yields checkpoints; feeds or other non-resumable sources need not support mid-fetch resume. `check`
must return a `CheckResult` for valid and invalid fixture credentials.

At runtime, `config_model` validates provider settings. The form renders the static `config_schema`
from the bundled manifest or a drop-in's `manifest.json`; a parity test checks bundled manifests
against runtime model schemas. Settings schemas use documented scalar fields and mark secret fields
`writeOnly`.

Scraping providers need recorded HTML fixtures, including a changed structure that raises
`StructureChangedError`, and may not add a solver dependency to the import graph. Providers declaring
`now_playing` need recorded active and idle fixtures, deterministic output, and correctly classified
structure, authentication, and rate-limit failures. Providers without that capability skip these
checks.

---

## 6. Acquisition hierarchy — a review checklist item

A provider must use the highest available surface, and its pull request must state which
higher surfaces were evaluated and why each was insufficient:

1. Official API with documented terms
2. Authenticated feed or export endpoint the platform provides
3. Public feed (RSS, JSON)
4. User-supplied export file (`file_import`)
5. Scraping

Where a higher tier covers part of the data — a feed for recent activity, an export for history — the
provider combines them rather than scraping what a feed already provides.

A scraping provider may authenticate as the operator, using the operator's own credentials, to
retrieve **that operator's own data** from a platform where they hold an account. It must not bundle
or share credentials, read other users' data, circumvent paywalls or access controls, defeat
anti-bot measures, or retrieve anything the authenticated operator could not see in their own
browser.

## 7. Migrating a provider to a better surface

If a platform adds a better acquisition surface, update the existing provider where practical.
Keep `id` and `native_id` stable where possible so existing rows continue to match. If payload
mapping changes, bump `schema_version` and ensure `normalize` can still handle retained raw payloads.
If native IDs differ between surfaces, document the change and plan for a full resync.
