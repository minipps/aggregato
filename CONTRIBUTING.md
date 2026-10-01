# Contributing to Aggregato

Aggregato collects the operator's own media logs from services where they have an account. Read the
acquisition and scraping rules before writing a provider.

The normative documents are [the engineering guidance](AGENTS.md) and
[the provider contract](docs/contracts/provider-plugin.md). This file is
the provider review checklist.

---

## The acquisition hierarchy

A provider MUST use the highest available acquisition surface:

1. **Official API** with documented terms
2. **Authenticated feed or export endpoint** the platform provides
3. **Public feed** (RSS, JSON)
4. **User-supplied export file** (`file_import`)
5. **Scraping**

Your pull request MUST state which higher surfaces you evaluated and why each was insufficient.
"I did not check" is not an answer; neither is "the API needs a key" unless the key is
unobtainable for an ordinary account holder.

Where a higher tier covers *part* of the data, combine tiers rather than dropping to the lowest
one. A provider may retain the generic host import path when a genuinely distinct local export is
needed; Letterboxd's RSS already covers its bundled surface, so it does not expose a second import
reader for the same payload.

`acquisition` is declared on the provider and surfaced in the UI at the moment the operator
enables it. It is a promise to the operator, so it must match what the code does.

---

## Scraping policy

Scraping is the last tier, and it is fenced. A scraping provider MAY authenticate as the operator,
using the operator's own credentials, to retrieve **that operator's own data** from a platform they
hold an account with.

It MUST NOT:

- bundle, share, or hard-code credentials of any kind;
- read any user's data other than the operator's own;
- circumvent a paywall or any access control;
- **defeat, solve, or work around a CAPTCHA or anti-bot measure, in any form.** This is a hard
  line. Encountering one means raising `BlockedError`; the run stops and
  the provider goes `degraded` immediately. A dependency whose purpose is solving challenges must
  not appear anywhere in the import graph — the conformance suite checks for this.
- retrieve anything the authenticated operator could not see in their own browser.

Bundled providers must use `ctx.http`. That host client applies the rate
`max(your declared rate, the floor for your acquisition mode)`, allows one in-flight request per host
for scrapers, and honors `Retry-After`. The import-linter contract rejects direct `httpx` imports.
Python providers are not sandboxed, so review drop-in source before enabling it.

A scraping provider must ship recorded HTML fixtures, including one with a changed structure that
raises `StructureChangedError`. A structural failure must raise that error rather than return an
empty result, which could be mistaken for an empty history.

---

## The hard rules for provider code

| Rule | Why |
|---|---|
| `normalize` is pure — no network, clock, randomness, or storage | Enables offline testing and replay; conformance checks compare repeated output for the same fixture |
| A provider has no database engine and imports only the selected provider | The child process limits provider access, but is not an OS sandbox; drop-ins retain service filesystem and network permissions |
| A provider never constructs its own HTTP client | The host client enforces request pacing and rate limits |
| A provider uses the closed `media_type`, `role`, and `subject_ref` vocabularies | Invalid records are stored as ingest failures rather than written |
| Extract every identifier found in the payload | Aggregato does not enrich records from third-party metadata sources |
| `logged_precision` is required on every entry | This preserves the precision the platform supplied |
| `role_raw` preserves the platform's role term verbatim | The term can be used when the normalized role vocabulary changes |
| `now_playing` is an explicit optional capability | A provider returns one normalized current item or `None`; playback is transient and never becomes history |

---

## Checklist before you open the pull request

- [ ] Acquisition surfaces evaluated, and the reasoning is in the PR description
- [ ] `uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run lint-imports`
- [ ] `uv run pytest` — including `tests/conformance/` with your provider registered
- [ ] Fixtures recorded per [tests/fixtures/README.md](tests/fixtures/README.md), redacted, no credentials
- [ ] `config_model` fields documented; secrets marked so the settings form renders them write-only
- [ ] New behaviour has a test that failed before the change (testing guidance)
- [ ] If `now_playing` is declared, static/runtime capability parity, active/idle/structure-change
      fixtures, deterministic normalization, and classified rate-limit/auth tests are present; the
      monitor/WebSocket tests use injected clocks and never sleep or open sockets
- [ ] Any migration follows [data-model.md §6](docs/data-model.md#migrations):
      foreign keys off around a SQLite table rebuild, a test that seeds rows at the previous revision
      and asserts they survive, and no edits to a revision that has already been applied
- [ ] Any new dependency justified against stdlib, native platform features, and what is already installed

## Cutting a release

`scripts/release.sh <version>` — updates the package versions, lockfiles, and documented image
examples, then creates the release commit and tag. Pushing the tag *is* the release
([release.yml](.github/workflows/release.yml)): it runs the same CI gates as a push to main, then
publishes a source archive, a GitHub release, and multi-arch images tagged `x.y.z`, `x.y`, `latest`.

The script exists for one reason worth knowing: the workflow refuses a tag whose version disagrees
with those two files, and by then the tag is already public. It runs that same check locally first,
alongside refusing a dirty tree, a branch other than main, a main that differs from origin, and a tag
that already exists. It shows what will ship and asks before pushing anything.
