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

## Koito

Koito is self-hosted, so its API is the highest surface and already the one in use:
`GET /apis/web/v1/listens`, authenticated with `Authorization: Token <api-key>`.

`listenbrainz` with a different `base_url` is deliberately *not* how Koito is read. Koito's
ListenBrainz compatibility covers submission only — `/apis/listenbrainz/1` serves `submit-listens`
and `validate-token` and exposes nothing that reads a history — so the two providers stay separate
with separate ids. `GET /apis/web/v1/export` is not used either: it streams the whole archive in one
unresumable response, which is a lower surface than a paged endpoint, not a higher one.

Paging walks backwards in time using the inclusive `to` filter, one second below the oldest listen
already emitted, and checkpoints after every page. Two consequences worth knowing before changing it:

1. `from` is always sent. Koito only honours `to` when `from` is non-zero; `to` alone resolves to
   `BETWEEN 0 AND 0` and answers with an empty page, which is indistinguishable from the end of the
   history.
2. The page boundary therefore has one-second resolution. If Koito grows a keyset cursor over
   `(listened_at, track_id)` — the shape its export path already uses internally — switch to it,
   keep `id="koito"`, and no resync is needed because the native id is unchanged.

Identifiers are installation-scoped integers (`koito_track`, `koito_artist`). A listen carries no
release, no rating and no review, so the provider declares none of those capabilities. If a future
Koito release states `musicbrainz_id` on the trimmed track and artist objects a listen carries, the
normalizer already files it (`mbid_recording`, `mbid_artist`) — record a fixture and bump
`schema_version` to replay stored payloads through it.

## Letterboxd

Letterboxd already supports automatic recent synchronization through its public RSS feed. Configure
either `username` (which derives the public RSS URL) or `rss_url`; the provider polls that feed at
its declared six-hour interval. A supplied `.rss`/`.xml` file uses the same parser for history.

If Letterboxd offers a documented complete export endpoint or a richer official API later, its
reader should replace only the acquisition adapter. Preserve `id="letterboxd"` and RSS GUIDs where
available; otherwise bump `schema_version`, document the full-resync changeover, and retain the RSS
normalizer for replay of stored historical payloads.
