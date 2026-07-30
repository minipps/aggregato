# Contributing to Aggregato

Aggregato aggregates one person's own media logs from platforms they hold accounts with. That
framing is what makes the project defensible, and the two policies below are what keep it true.
Read them before writing a provider.

The normative documents are [the constitution](.specify/memory/constitution.md) and
[the provider contract](specs/001-media-log-aggregator/contracts/provider-plugin.md). This file is
the review checklist derived from them.

---

## The acquisition hierarchy

A provider MUST use the highest surface available to it (FR-042):

1. **Official API** with documented terms
2. **Authenticated feed or export endpoint** the platform provides
3. **Public feed** (RSS, JSON)
4. **User-supplied export file** (`file_import`)
5. **Scraping**

Your pull request MUST state which higher surfaces you evaluated and why each was insufficient.
"I did not check" is not an answer; neither is "the API needs a key" unless the key is
unobtainable for an ordinary account holder.

Where a higher tier covers *part* of the data, combine tiers rather than dropping to the lowest
one. Letterboxd is the worked example: RSS for recent activity, the user's CSV export for
history — not a scraper for what the feed already provides.

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
  line, not a default (FR-044). Encountering one means raising `BlockedError`; the run stops and
  the provider goes `degraded` immediately. A dependency whose purpose is solving challenges must
  not appear anywhere in the import graph — the conformance suite checks for this.
- retrieve anything the authenticated operator could not see in their own browser.

Politeness is enforced by the host, not by you: the rate limiter runs at
`max(your declared rate, the floor for your acquisition mode)`, scraping providers get one
in-flight request per host, and `Retry-After` is honoured for you. You cannot raise the floor, and
a unit test asserts you cannot. Do not build your own client — `ctx.http` is the only one, and an
import-linter contract fails the build if `aggregato/providers/*` imports `httpx`.

A scraping provider must ship recorded HTML fixtures, including one with a changed structure that
raises `StructureChangedError`. Returning empty on a structural failure is forbidden: silence plus
delete inference is how an archive gets erased (FR-024, FR-026).

---

## The hard rules for provider code

| Rule | Why |
|---|---|
| `normalize` is pure — no network, no clock, no randomness, no storage | Makes replay (FR-002) and offline credential-free tests (FR-036) possible. Enforced: called twice on one fixture must produce identical output |
| A provider never receives a database handle or another provider's secrets | FR-037. Enforced physically: provider code runs in a child process with no engine and no other provider imported |
| A provider never constructs its own HTTP client | FR-043 politeness floors must be un-overridable |
| A provider may not invent a `media_type`, `role`, or `subject_ref` key | FR-008. Violations become `ingest_failures`, not writes |
| Every identifier in a payload is extracted, including ones Aggregato has no use for | FR-009 — the single largest lever on match quality, because Aggregato never enriches from third-party metadata sources |
| `logged_precision` is required on every entry, with no default | A default would silently fabricate exactness (FR-004) |
| `role_raw` carries the platform's own word verbatim, always | FR-016 — the vocabulary widens later and the raw term is what replay re-derives from |

---

## Checklist before you open the pull request

- [ ] Acquisition surfaces evaluated, and the reasoning is in the PR description
- [ ] `uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run lint-imports`
- [ ] `uv run pytest` — including `tests/conformance/` with your provider registered
- [ ] Fixtures recorded per [tests/fixtures/README.md](tests/fixtures/README.md), redacted, no credentials
- [ ] `config_model` fields documented; secrets marked so the settings form renders them write-only
- [ ] New behaviour has a test that failed before the change (Constitution II)
- [ ] Any migration follows [data-model.md §6](specs/001-media-log-aggregator/data-model.md#migrations):
      foreign keys off around a SQLite table rebuild, a test that seeds rows at the previous revision
      and asserts they survive, and no edits to a revision that has already been applied
- [ ] Any new dependency justified against stdlib, native platform features, and what is already installed

## Cutting a release

`scripts/release.sh 0.1.1` — bumps `pyproject.toml` and `frontend/package.json` (plus the lockfile),
commits, and pushes the tag. Pushing the tag *is* the release
([release.yml](.github/workflows/release.yml)): it runs the same CI gates as a push to main, then
publishes a source archive, a GitHub release, and multi-arch images tagged `x.y.z`, `x.y`, `latest`.

The script exists for one reason worth knowing: the workflow refuses a tag whose version disagrees
with those two files, and by then the tag is already public. It runs that same check locally first,
alongside refusing a dirty tree, a branch other than main, a main that differs from origin, and a tag
that already exists. It shows what will ship and asks before pushing anything.
