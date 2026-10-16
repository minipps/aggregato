# Provider acquisition roadmap

Keep one stable provider id per service as its acquisition surface improves. Imports and
automatic synchronization therefore normalize into the same `provider_item` identity space rather
than creating a replacement provider or duplicating an archive.

## Spotify

The provider uses Spotify's user-authorized Web API, `GET /v1/me/player/recently-played`, with
`user-read-recently-played`. Its 50-item pages have time cursors, so polling and backfill use the
same endpoint. Configure the app ID, secret, and refresh token from Spotify's
[Authorization Code flow](https://developer.spotify.com/documentation/web-api/tutorials/code-flow);
the refresh token must be authorized with that scope. Development Mode requires a Premium app
owner and an allowlisted user. The provider records tracks only; Spotify's separate lifetime export
is outside this API provider.

## Goodreads

Goodreads has no supported personal-library API. The provider uses the public
`/review/list_rss/<user id>` feed as its sole bundled acquisition surface. The feed is a rolling
100-item snapshot, not a full-history backfill; its records preserve Goodreads `Book Id`, ISBN,
ISBN13, ratings, reviews, shelves, and the distinction between `Date Read` and `Date Added`.

When Goodreads provides a supported personal-library feed or export endpoint, the provider will:

1. Implement a reader for that documented payload and map it into the existing raw-book shape.
2. Keep `poll` only with recorded fixtures and an offline conformance case.
3. Keep `id="goodreads"`, `goodreads` Book Ids, ISBN identifiers, and the rating scale unchanged.
4. Bump `schema_version` and require a full resync only if the new endpoint exposes different
   native book identifiers.

Retain historical payload support when changing acquisition surfaces.

## Backloggd

Backloggd's public documentation does not publish a public API or authenticated feed/export
endpoint for account history. Its [roadmap](https://backloggd.com/roadmap/) still lists CSV export
as a requested feature. The highest documented account-history surface is the public Reviews RSS
feed at `/u/<username>/reviews/rss/`. The [changelog](https://backloggd.com/changelog/) says this
feed contains only the user's latest 20 reviews. Configure the provider with the username from your
own profile; it derives the feed URL and polls every six hours. No account credential is needed for
this public feed, and Backloggd does not document an RSS request limit.

```yaml
providers:
  backloggd:
    enabled: true
    settings:
      username: your_backloggd_username
```

The RSS 2.0 items expose a review GUID, title, optional review link, feed `pubDate`, review text,
numeric `backloggd:user_rating` (namespace URI `https://backloggd.com/`), reviewer, and optional
cover image. The review GUID is the native review identity and should remain the `provider_item`
identity; the feed has no native game identifier, so game identity is left to host resolution from
the title and release year. `pubDate` is the feed item's publication time, not a documented play
date.

This surface supports review and rating synchronization only. It does not expose the user's full
log history, journal sessions, play dates, play status, backlog or wishlist, and it cannot backfill
past the latest 20 reviews. The provider must not infer played entries from reviews. Full fetches
read the same bounded feed and must not be presented as complete-history backfills. The official
[About page](https://backloggd.com/about/) describes logs and journals separately from reviews and
allows multiple logs for a game, which confirms that the RSS feed is not a complete activity
archive.

`tests/fixtures/backloggd/reviews.rss` is a sanitized, user-supplied recording of a real 20-item
feed response. It preserves the feed structure and item order while replacing the source account
name and per-review identifiers with placeholders; no original account identity or review IDs are
stored in the repository. The recording verifies one actual rolling-feed snapshot, not full-history
coverage or behavior across account visibility settings. CI reads this fixture offline and never
captures a live feed. The empty-feed envelope, missing-user status, and block/throttle responses are
not defined by Backloggd's public documentation; their fixtures are synthetic.

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

Letterboxd already supports synchronization through its public RSS feed. Configure either
`username` (which derives the public RSS URL) or `rss_url`; the provider polls that feed at its
declared six-hour interval. The bundled provider does not add a second local-file reader for the
same RSS payload.

If Letterboxd offers a documented complete export endpoint or a richer official API later, its
reader should replace only the acquisition adapter. Preserve `id="letterboxd"` and RSS GUIDs where
available; otherwise bump `schema_version`, document the full-resync changeover, and retain the RSS
normalizer for replay of stored historical payloads.

## Serializd

Serializd's [official homepage and FAQ](https://www.serializd.com/) describe show tracking, ratings,
reviews, and a TV diary. They list imports from TV Time and Trakt, but no outgoing Serializd export.
The official material reviewed does not publish an account-history API, authenticated feed/export,
or public RSS/JSON feed. Its inbound import feature does not provide a user export. The provider
therefore uses the authenticated internal JSON routes observed in Serializd's
[shared frontend bundle](https://www.serializd.com/_next/static/chunks/pages/_app-9bc50fbf3b31c617.js)
and [diary page bundle](https://www.serializd.com/_next/static/chunks/pages/user/UserDiary-adbc6b5301b32ead.js)
on 2026-10-15, and is classified as `scrape`; these routes are first-party implementation details,
not a supported public API. The inspected frontend falls back to
`https://serializddesktop.onrender.com/api` and allows a build-time backend override.

Configure the username and a secret token from **your own** signed-in Serializd browser session. In
the browser's Network tools, copy the `tvproject_credentials` cookie value to an environment variable;
the provider sends it as a bearer token. It does not submit an email or password. Treat the cookie
value like a login credential and keep it out of logs, fixtures, and source control.

The provider calls `/api/user_information?shouldGetUserContext=true`, matching the logged-in UI's
identity check. It compares the returned `username` with the configured name and discards other
user-context fields. It then reads `/api/user/{username}/diary?page=N&include_target=ALL`, whose
response has page-numbered `reviews` and `totalPages`. The provider reads all reported pages from
page 1 on each run, including entries with the show's rating, review, like, spoiler, rewatch, and
season/episode fields when present. A row marked `isLog=false` retains its opinion and withdraws its
watch entry; the host tombstones a prior entry with that identity. Rows without a valid `isLog` flag
raise `StructureChangedError` rather than being guessed to be watches. It remains unverified whether
`include_target=ALL` returns every rating-only row for every account. Pages have no stable cursor or
checkpoint, so runs cannot resume partway through;
new diary rows may shift pages during a fetch and cause overlap or gaps until a later full reread. A
six-hour poll is a project cadence, since Serializd publishes no request limit for this route. A full
run covers the pages reported by the route, not a transactionally consistent snapshot. No official
maximum history window is documented. Keep `infer_deletes` disabled: a shifted page walk is not a
stable snapshot from which to infer removals.

When “Add to Diary” is on, the app's [review composer](https://www.serializd.com/_next/static/chunks/7053-3ba2623410930c77.js)
offers a date-only “Watched on” picker. For logged records, the provider takes the calendar date from
`backdate`, stores it at UTC midnight, and sets `logged_precision=day`; it retains `dateAdded` as raw
data without treating it as watch time. Rating-only rows do not require `backdate`. Special episodes
can belong to season 0 in Serializd, while Aggregato permits only positive season references. A
season-zero entry, or an episode with no resolvable positive season, fails normalization individually
and remains in `ingest_failures` with its raw payload instead of being reassigned to another season.

The fixtures are synthetic examples, not sanitized recordings. No account request or live
compatibility check has been made, so the observed route and response assumptions still need
validation against a sanitized response from an operator's own account. The synthetic fixtures do not
establish exact live timestamp serialization or which diary fields may be null for a particular
account.

## YouTube Music

No official YouTube Music history feed or YouTube Data API history method is documented. The YouTube
Data API does not provide a supported way to read a user's watch history;
[`playlistItems.list`](https://developers.google.com/youtube/data/reference/rest/v3/playlistItems/list)
documents `watchHistoryNotAccessible` for watch-history playlists. YouTube Music
does share watch-history settings with YouTube, and Music Recap uses listening history across YouTube
platforms, so an official account-history export is a plausible higher-tier source. The Data
Portability API's restricted `dataportability.myactivity.youtube` scope exports timestamped YouTube
activity, with generic fields such as `header`, `title`, `titleUrl`, `time`, and `products`. Its YouTube
examples use a generic YouTube header, URL, and product label, and the schema says fields vary by
service and action; it does not specify a YouTube Music listen marker or a per-play event identifier.
Although exported timestamps may distinguish separate plays, the schema does not establish which
records are Music listens rather than other YouTube activity.
The separate `dataportability.youtube.music` scope exports uploaded tracks and the Music library, not
listening history. This is a data-coverage limitation, not just OAuth setup friction. Google Takeout
supports user-supplied YouTube and My Activity exports, but its user
documentation does not publish a distinct Music-history JSON shape; no sanitized real Takeout archive
has established one here. Revisit a file-import or Data Portability backfill if a real, sanitized
export validates one. See Google's [Takeout instructions](https://support.google.com/accounts/answer/3024190),
[activity schema](https://developers.google.com/data-portability/schema-reference/my_activity), and
[YouTube/Music schema](https://developers.google.com/data-portability/schema-reference/youtube).

Data Portability is available to consumer accounts only in Google's listed countries and regions;
managed accounts can be restricted by their administrator, and the API excludes accounts under 18.
Google's API quickstart uses a developer-organization-controlled account for testing. The general
OAuth policy separately allows personal-use and development/testing exceptions; an external test app
must allowlist its users (up to 100), and its refresh tokens expire after seven days. A public
production app using the restricted scope requires Google's OAuth verification and may require a
recurring security assessment. See the [API quickstart](https://developers.google.com/data-portability/user-guide/quickstart),
[availability and account
limits](https://support.google.com/accounts/answer/14452558), [scope list](https://developers.google.com/data-portability/user-guide/scopes),
[OAuth testing rules](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview),
and [restricted-scope review](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).

At the user's explicit direction, this provider uses the authenticated, unofficial YouTube Music web
history route implemented by `ytmusicapi.get_history`.
That method requests the `FEmusic_history` browse result and reads its music shelves; the `played`
value is the shelf's date label. The provider classifies this as authenticated `scrape`, reproduces
only the read-only request through `ctx.http`, and adds no `ytmusicapi` dependency. The route is
undocumented and can change. At upstream commit
[`4da34b68e68d33356860fbf5d4f0245655969b08`](https://github.com/sigma67/ytmusicapi/tree/4da34b68e68d33356860fbf5d4f0245655969b08),
[`get_history`](https://github.com/sigma67/ytmusicapi/blob/4da34b68e68d33356860fbf5d4f0245655969b08/ytmusicapi/mixins/library.py)
makes one browse request and does not follow continuations. The upstream
[`get_history` test](https://github.com/sigma67/ytmusicapi/blob/4da34b68e68d33356860fbf5d4f0245655969b08/tests/mixins/test_library.py)
uses a live authenticated account, and the upstream [test data](https://github.com/sigma67/ytmusicapi/tree/4da34b68e68d33356860fbf5d4f0245655969b08/tests/data)
contains no history response. Aggregato's fixtures are synthetic; they do not verify live response
coverage, and a sanitized successful `FEmusic_history` recording is still needed. An unexpected
response shape must fail the run rather than be treated as an empty history.

Configure an authenticated browser session from the same account whose history you want to collect.
In a browser signed into [music.youtube.com](https://music.youtube.com), open Developer Tools →
Network, find a successful `POST` to `/browse`, and copy the complete `Cookie` request-header value
and the `X-Goog-AuthUser` value (usually `0`); see upstream's [browser-auth instructions](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html).
Pass the full Cookie header through a secret environment variable; do not commit it, paste it into
issues, or include it in fixtures. Set `timezone` to the account's IANA timezone so relative day
labels resolve correctly; it defaults to UTC.

```yaml
providers:
  youtube_music:
    enabled: true
    settings:
      cookie: ${YTMUSIC_COOKIE}
      auth_user: "0"
      timezone: Europe/Madrid
```

The provider polls one rolling history snapshot every 15 minutes, a project-chosen cadence rather
than a YouTube-published request limit. The upstream method makes one request and does not follow
continuations, so this surface is not a complete-history backfill. It returns a day-group label
(`Today`, `Yesterday`, a weekday, or a calendar date), not a play time. Fetch resolves the day using
the response's HTTP `Date` and configured timezone; records use `logged_precision=day`. The source
provides a `videoId` but no per-listen id or timestamp, so repeated plays of the same video on one day
can collapse into one record. Keep `infer_deletes` disabled: absence from this snapshot is not evidence
of deletion, and a `full` fetch sees the same bounded history window.
