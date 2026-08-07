# Contract: Provider Plugin

**Version**: provider API 1.0 | **Date**: 2026-07-29

This is the extension contract plugin-system guidance governs. It is a **public API**: changes
follow semantic versioning, a MAJOR bump needs a migration note, and the conformance suite is updated
in the same change.

Bundled providers ship with the core , so they are version-locked by construction. The
version range check exists only for drop-in development providers and logs a warning rather than
gating — deliberately low-priority in M1, because the case does not exist yet.

---

## 1. The interface

```python
class Provider(Protocol):
    id: str                          # stable slug, e.g. "letterboxd" — never changes, ever
    name: str                        # display name
    media_types: set[MediaType]      # from the closed enum; a provider cannot invent one
    capabilities: set[Capability]
    acquisition: Acquisition         # api | feed | export | scrape
    config_model: type[BaseModel]    # validation AND the generated settings form 
    rating_scales: list[RatingScale]
    schema_version: int              # bump to trigger normalization replay 
    default_poll_interval: timedelta # from the platform's own rate limits, not a global default

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]: ...

    def normalize(self, raw: RawRecord) -> NormalizedBatch: ...

    async def check(self, ctx: ProviderContext) -> CheckResult: ...
```

`FetchMode` ∈ `{incremental, full, import}`. In `import` mode, `ctx.import_path` is set.

`fetch` may yield a `Checkpoint` between records; the host persists the enclosed cursor so a failed
run resumes from there . A provider whose pagination cannot support mid-fetch resumption
declares only `full` and accepts full resyncs.

### Hard rules

| Rule | Why | How it is enforced |
|---|---|---|
| `normalize` is **pure** — no network, no clock, no randomness, no storage | Makes replay  and credential-free offline tests  possible | Conformance suite: called twice on one fixture must produce identical output; sockets blocked; clock frozen |
| During a normal scheduled run, a provider receives no database handle and the host passes only the selected provider's context |  | The parent owns the engine and sends the selected configuration through the child protocol. Discovery reads static manifests without importing provider packages. The runner passes a minimal explicit runtime environment. This is a process boundary, not an OS sandbox: unreviewed drop-ins may still access the child's filesystem and network permissions. |
| A provider never constructs its own HTTP client |  politeness floors must be un-overridable | `ctx.http` is the only client; an import-linter rule forbids importing `httpx` from `aggregato/providers/*` |
| A provider may not invent a `media_type`, `role`, or `subject_ref` key |  | Parent-side validation at the ingest boundary; violations become `ingest_failures`, not writes |
| Every identifier present in a payload is extracted, including ones Aggregato has no use for | ; the single largest lever on match quality under the no-enrichment rule | Conformance suite asserts identifiers visible in the fixture appear in the output |
| No CAPTCHA or anti-bot circumvention, in any form |  — a hard line, not a default | Raise `BlockedError`; the run stops and the provider goes to `degraded` immediately |

---

## 2. What the host provides

`ProviderContext`:

| Field | Guarantee |
|---|---|
| `http` | `httpx.AsyncClient` wrapped with rate limiting at `max(declared, host_floor)`, one in-flight request per host within that run for `scrapes` providers, retry on 5xx/429/transport with jitter, `Retry-After` compliance, ETag pass-through, and the project User-Agent with a contact URL |
| `config` | The validated `config_model` instance for this provider only |
| `secrets` | The selected provider's credentials, resolved before the child starts and sent explicitly; unreviewed drop-ins are not an OS sandbox and may still access filesystem and network permissions |
| `log` | Logger already bound to `provider_id`, `run_id`, `lineage_id` |
| `state` | Small opaque key/value store, this provider's own, persisted in `provider_state.kv` |
| `import_path` | Set only in `import` mode |

The host owns, and a provider must not reimplement: scheduling, jitter, the retry ladder, rate
limiting, cursor persistence, idempotency, identity resolution, storage, migrations, and image
caching .

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

- `logged_precision` is **required** on every entry. There is no default, because a default would
  silently fabricate exactness .
- `rating_raw` requires a `rating_scale_id` the provider declared in `rating_scales`.
- `subject_ref` uses only `season`, `episode`, `track`, `disc`, `chapter`, `volume`, positive integers.
- `position` on credits: payload order where the platform does not express billing .
- `role_raw` carries the platform's own word verbatim, always, even when `role` maps cleanly.
- A provider that finds nothing must return an empty iterator. It must **not** report success on a
  structural failure — raise `StructureChangedError` instead, because silence plus delete inference is
  how an archive gets erased .

---

## 4. Error signalling

Raise, don't return. The host classifies and decides retry policy:

| Exception | `error_class` | Host behaviour |
|---|---|---|
| `AuthError` | `auth` | **No retry.** Degraded immediately; retrying risks account lockout |
| `BlockedError` | `blocked` | **No retry.** Degraded immediately; retrying deepens the block |
| `StructureChangedError` | `structure_changed` | **No retry.** UI says "this provider needs updating" with an issue-tracker link |
| `RateLimited(retry_after=…)` | `rate_limit` | Retries, honours `Retry-After`, lengthens the effective interval for the rest of the session |
| `httpx` transport errors, 5xx | `transport` | Retried in-run, then the ladder across runs |
| Validation failure on one record | `parse` | That record goes to `ingest_failures`; the run continues |
| Anything else | `internal` | Ladder; full traceback in the run's `log_excerpt` |

---

## 5. Conformance suite — a merge gate

Every bundled provider is registered into `tests/conformance/` and must pass. This is the contract
testing guidance requires, and the compliance proof requires.

Asserted for every provider:

1. `id` is a stable slug; `schema_version` ≥ 1; `default_poll_interval` is set.
2. Declared `media_types`, `capabilities`, and `acquisition` are consistent with observed behaviour —
   a provider claiming `has_ratings` must produce at least one opinion from its fixtures.
3. `normalize` purity: identical output across two calls; sockets blocked; clock frozen; no writes.
4. Every `media_type`, `role`, and `subject_ref` key produced is in the closed vocabulary.
5. Every identifier visible in the fixture appears in the output.
6. Cursors round-trip: `fetch` → checkpoint → resume produces no duplicate and no gap.
7. `check` returns a `CheckResult` for both a valid and an invalid fixture credential set.
8. `config_model` produces a JSON Schema the settings form can render: flat fields, secrets marked
   `writeOnly`, every field documented.
9. **Scraping providers additionally**: recorded HTML fixtures present; a fixture with a changed
   structure raises `StructureChangedError` rather than returning empty; no solver dependency in the
   import graph.

Tests run offline with no credentials. A network call in a conformance test is a failure, not a slow
test.

---

## 6. Acquisition hierarchy — a review checklist item

A provider must use the highest surface available , and its pull request must state which
higher surfaces were evaluated and why each was insufficient:

1. Official API with documented terms
2. Authenticated feed or export endpoint the platform provides
3. Public feed (RSS, JSON)
4. User-supplied export file (`file_import`)
5. Scraping

Where a higher tier covers part of the data — a feed for recent activity, an export for history — the
provider combines them rather than scraping what a feed already provides.

A scraping provider may authenticate as the operator, with the operator's own credentials, to retrieve
**that operator's own data** from a platform they hold an account with. It may not bundle or share
credentials, read other users' data, circumvent paywalls or access controls, defeat anti-bot measures,
or retrieve anything the authenticated operator could not see in their own browser .

## 7. Migrating a provider to a better surface

When a platform ships an API later, the provider migrates in place rather than being replaced
. `id` and `native_id` values stay stable where possible so existing entries relink instead of
duplicating. Where native ids differ between surfaces, bump `schema_version` and document the
changeover as requiring a full resync; retained raw payloads keep the old data interpretable either
way.
