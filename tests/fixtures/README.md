# Recorded fixtures

Every test in this repository reads its data from here. Nothing touches the network and nothing
needs credentials  — `tests/conftest.py` blocks sockets outright, so a test that
tries is a failure rather than a slow test.

## Conventions

- **One directory per provider**, named exactly as the provider's `id`: `listenbrainz/`,
  `anilist/`, `goodreads/`, `letterboxd/`, `fixture/`.
- **One file per acquisition response**, in the wire format that provider actually receives:
  `.json` for APIs, `.jsonl` for paged streams (one page per line), `.csv` for export files,
  `.xml` for feeds, `.html` for scraped pages.
- **Names describe the case, not the ordinal**: `page-1.json`, `rating-ordinal.json`,
  `structure-changed.html`, `credentials-invalid.json`. A reader should know what a fixture proves
  before opening it.
- **No credentials, ever.** Tokens, cookies, session ids, e-mail addresses, and user ids are
  replaced with obvious placeholders (`REDACTED`, `user-0001`). Recording script output is reviewed
  by hand before it is committed.
- **No live capture in CI.** Fixtures are recorded once, by hand, by a contributor with an account,
  and committed. CI never records; there is no network in CI.
- **Verbatim payloads.** Do not prettify, reorder, or trim a recorded payload beyond redaction —
  identifier extraction  and `StructureChangedError` detection both depend on the real
  shape.

## What each provider directory must contain

The conformance suite (`tests/conformance/`, contracts/provider-plugin.md §5) reads these, so a
provider is not conformant without them:

| Fixture | Proves |
|---|---|
| at least two pages | cursors round-trip with no duplicate and no gap (group 6) |
| one record per declared capability | declared capabilities match observed behaviour (group 2) |
| one record carrying every identifier the platform exposes | identifier extraction (group 5) |
| a valid and an invalid credential response | `check` returns a `CheckResult` for both (group 7) |
| **scraping providers only**: a structurally-changed page | `StructureChangedError` rather than a silent empty result (group 9) |

## Recording a fixture

Record against your own account only, at the highest acquisition surface the provider uses
(CONTRIBUTING.md), redact, then commit. State in the pull request which account type produced it —
an empty history and a ten-year history exercise different code.
