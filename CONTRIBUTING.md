# Contributing to Aggregato

Aggregato collects the operator's own media logs from services where they have an account. Read the
acquisition and scraping rules before adding a provider.

The [provider contract](docs/contracts/provider-plugin.md) defines the implementation requirements;
this guide covers acquisition policy, safety, and the contribution checklist. See
[docs/validation.md](docs/validation.md) for development and quality-check commands.

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
for scrapers, and honors `Retry-After`. Providers cannot import `httpx` directly. Python providers
are not sandboxed, so inspect drop-in source before enabling it.

A scraping provider must ship recorded HTML fixtures, including one with a changed structure that
raises `StructureChangedError`. A structural failure must raise that error rather than return an
empty result, which could be mistaken for an empty history.

---

## Provider checklist

| Requirement | Reason |
|---|---|
| Keep `normalize` pure: no network, clock, randomness, or storage | Stored payloads can be replayed deterministically |
| Use the closed `media_type`, `role`, and `subject_ref` vocabularies | Invalid records are retained as ingest failures instead of being written |
| Extract every identifier and preserve `logged_precision` and `role_raw` | Aggregato does not enrich from third-party metadata |
| Declare optional capabilities such as `now_playing` accurately | Capabilities control the UI and runtime behavior |

---

## Before opening a pull request

- [ ] Explain which acquisition surfaces you checked and why the selected surface is appropriate.
- [ ] Record provider fixtures under [tests/fixtures](tests/fixtures/README.md), sanitized and without credentials.
- [ ] Document `config_model` fields and mark secrets so the settings form keeps them write-only.
- [ ] Add a deterministic test for new behavior; run the checks in [docs/validation.md](docs/validation.md).
- [ ] Follow [the migration rules](docs/data-model.md#migrations); migrations are forward-only.
- [ ] Justify each new dependency against the standard library and installed packages.

## Cutting a release

Run `scripts/release.sh <version>` from a clean `main` that matches `origin/main`. It updates package
versions, lockfiles, and the image-version examples, then shows the release commit and tag for review.
Pushing the tag starts [the release workflow](.github/workflows/release.yml), which runs CI and
publishes the source archive, GitHub release, and multi-architecture images.
