# Backloggd fixtures

`reviews.rss` is a user-supplied recording from Backloggd's public Reviews RSS feed. No live request
was made to collect it, and CI serves it only through `httpx2.MockTransport`. The recording retains
its original XML structure, order, whitespace, review text, titles, ratings, dates, links, and image
URLs. Sanitization replaced the account name with `user-0001` and replaced each review id in both
its GUID and review URL with a consistent sequential numeric placeholder (`9000001`–`9000020`). No
original account name or review id remains in the fixture.

Backloggd documents a rolling feed of at most 20 reviews, so this sample does not demonstrate a
full-history backfill. `pubDate` is the review's publication date and is normalized as opinion
authorship time; the provider creates no game-play entries. The recording includes the literal `½`
star symbol in titles to exercise title and year extraction.

The other files are synthetic edge cases, not recordings:

| File | Purpose |
|---|---|
| `empty.rss` | A valid RSS channel with no items. |
| `structure-changed.rss` | An item missing required review structure; fetch and `check()` must report a structural failure rather than empty history. |
| `missing-channel-title.rss`, `missing-channel-link.rss`, `missing-channel-description.rss` | Required RSS channel fields omitted one at a time. |
| `entry-shape-changed.rss` | A channel that replaces RSS `item` elements with an `entry` element. |
| `unknown-item-extension.rss` | An unknown namespaced item field retained in `RawRecord.payload` for normalization replay. |
| `review-text-challenge-words.rss` | A valid review containing words used to identify challenge pages; it must not be mistaken for a block response. |
| `blocked.html`, `blocked-200.html` | Challenge pages paired with HTTP 403 and HTTP 200 to verify the provider stops on a block at either status. |
| `rate-limited.txt` | HTTP 429 response body; the test transport adds `Retry-After: 120`. |

The provider has no credential fields, so the conformance check's negative case uses the malformed
RSS response rather than inventing a credential rejection.
