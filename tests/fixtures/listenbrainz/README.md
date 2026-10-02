# ListenBrainz fixtures — synthesised, not captured

These files are **hand-authored to the documented shape** of
`GET {base}/1/user/{username}/listens`, not recorded from a real account: no contributor account was
read, so there is no username, user id or token in this directory to redact — `listener-0001` and
`REDACTED` are placeholders (tests/fixtures/README.md). Every MBID is a syntactically valid UUID that
identifies nothing.

Shapes are otherwise verbatim: key names, nesting, the string-typed `tracknumber`, the integer
`caa_id`, and the absence of `mbid_mapping` on an unmatched listen are all as the platform sends
them, because identifier extraction  and `StructureChangedError` detection both depend on
the real shape.

| File | Proves |
|---|---|
| `page-1.json` | first page; the full identifier set with `mbid_mapping`; a multi-artist credit; a sparse listen with no `additional_info` |
| `page-2.json` | second page, so cursors round-trip with no duplicate and no gap (group 6) |
| `page-3-empty.json` | the terminal response that ends a backwards walk |
| `credentials-valid.json` | `check` against a working token (group 7) |
| `credentials-invalid.json` | the body ListenBrainz returns with HTTP 401 (group 7) |
| `structure-changed.json` | a response whose shape is genuinely wrong — must raise `StructureChangedError` |

The conformance registration serves `page-*.json` through an `httpx2.MockTransport` keyed on the
`max_ts` query parameter, so replaying them **checks** the backwards paging rather than just handing
back pages in call order.
