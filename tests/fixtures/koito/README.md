# Koito fixtures — synthesised, not captured

These files are **hand-authored to the shape Koito's Go handlers marshal**
(`GET {base}/apis/web/v1/listens` → `db.PaginatedResponse[*models.Listen]`), not recorded from a
real server: no contributor instance was read, so there is no username, user id or API key in this
directory to redact (tests/fixtures/README.md). Track and artist ids are small integers, as an
installation's own autoincrement ids are; the image UUID identifies nothing.

Shapes are otherwise verbatim: the `items` / `total_record_count` / `has_next_page` envelope, the
RFC 3339 `time`, the trimmed track object (`id`, `title`, `artists`, `image` — no
`musicbrainz_id`, no album), and the empty-string image list Koito sends for a track with no
artwork are all as the server sends them, because identifier extraction  and
`StructureChangedError` detection both depend on the real shape.

| File | Proves |
|---|---|
| `page-1.json` | first page; a single-artist listen and a two-artist credit; both track and artist ids |
| `page-2.json` | second page, so cursors round-trip with no duplicate and no gap (group 6); a track with no artists, and the same track listened to twice, which must yield two distinct native ids |
| `page-3-empty.json` | the terminal response that ends a backwards walk |
| `credentials-invalid.json` | the body Koito returns with HTTP 401 (group 7) |
| `structure-changed.json` | a response whose shape is genuinely wrong — `listens` where `items` belongs — which must raise `StructureChangedError` rather than read as an empty history |

The conformance registration serves `page-*.json` through an `httpx2.MockTransport` keyed on the `to`
query parameter, so replaying them **checks** the backwards paging rather than just handing back
pages in call order.
