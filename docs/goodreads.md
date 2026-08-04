# Goodreads RSS sync

Aggregato synchronizes Goodreads through its public bookshelf RSS endpoint,
`/review/list_rss/<user id>`. This is a feed, not an API or scraper.

## Configure the feed

In **Providers**, set Goodreads `profile_url` to either your normal profile URL or the RSS URL:

- `https://www.goodreads.com/user/show/155188990-mini`
- `https://www.goodreads.com/review/list_rss/155188990-mini`

Aggregato converts a profile URL to the RSS URL automatically. Enable the provider and select
**Sync now** for the first refresh; it then refreshes daily.

```yaml
providers:
  goodreads:
    profile_url: https://www.goodreads.com/user/show/155188990-mini
```

The feed contains ratings, reviews, shelves, book/review IDs, reading dates, publication year,
page count, and book artwork. It is refetched on every automatic run; Goodreads book IDs make the
writes idempotent.

## Full history

The RSS endpoint is a rolling snapshot. On 4 August 2026, the supplied feed contained 100 items,
so it must not be treated as a complete historical export. Import Goodreads' **Library Export CSV**
to capture the full archive (including ISBNs and older books), then configure RSS for ongoing
updates. An earlier CSV import cursor does not suppress RSS refreshes.

## Failures

- `auth`: Goodreads denied access to the feed.
- `blocked`: Goodreads returned a CAPTCHA or anti-bot page. Do not retry repeatedly or attempt to
  bypass it; use a Library Export CSV instead.
- `structure_changed`: Goodreads changed the RSS structure. The run stops rather than accepting an
  unfamiliar feed as an empty bookshelf.
