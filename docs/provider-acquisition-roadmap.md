# Provider acquisition roadmap

Phase 7 keeps one stable provider id per service as its acquisition surface improves. Imports and
automatic synchronization therefore normalize into the same `provider_item` identity space rather
than creating a replacement provider or duplicating an archive.

## Goodreads

Today Goodreads uses the operator's Library Export CSV. It preserves Goodreads `Book Id`, ISBN,
ISBN13, ratings, reviews, shelves, and the distinction between `Date Read` and `Date Added`.

`automatic_feed_url` is intentionally present in `GoodreadsConfig` but does not fetch yet. When
Goodreads provides a supported personal-library feed or export endpoint, the provider will:

1. Implement a reader for that documented payload and map it into the existing raw-book shape.
2. Enable `poll` only with recorded fixtures and an offline conformance case.
3. Keep `id="goodreads"`, `goodreads` Book Ids, ISBN identifiers, and the rating scale unchanged.
4. Bump `schema_version` and require a full resync only if the new endpoint exposes different
   native book identifiers.

That makes the future automatic path additive while refusing to pretend an unsupported endpoint is
safe to poll today.

## Letterboxd

Letterboxd already supports automatic recent synchronization through its public RSS feed. Configure
either `username` (which derives the public RSS URL) or `rss_url`; the provider polls that feed at
its declared six-hour interval. A supplied `.rss`/`.xml` file uses the same parser for history.

If Letterboxd offers a documented complete export endpoint or a richer official API later, its
reader should replace only the acquisition adapter. Preserve `id="letterboxd"` and RSS GUIDs where
available; otherwise bump `schema_version`, document the full-resync changeover, and retain the RSS
normalizer for replay of stored historical payloads.
