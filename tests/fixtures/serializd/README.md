# Serializd fixtures

Every file in this directory is synthetic. No Serializd account was used and no diary/API
response was captured. The fixture envelope and consumed field names follow Serializd's deployed
first-party frontend chunks:

- `https://www.serializd.com/_next/static/chunks/pages/user/UserDiary-adbc6b5301b32ead.js`
- `https://www.serializd.com/_next/static/chunks/pages/_app-9bc50fbf3b31c617.js`
- `https://www.serializd.com/_next/static/chunks/6925-6e760b97c1029904.js` (review fields)
- `https://www.serializd.com/_next/static/chunks/7053-3ba2623410930c77.js` (review composer)

The frontend reads diary pages as `{reviews, totalPages}`; renders each row's `id`, `rating`,
`showId`, `seasonId`, `showName`, `showPremiereDate`, `showSeasons`, `showBannerImage`, `backdate`,
`isRewatch`, `episodeNumber`, `reviewText`, `episodeName`, `like`, `containsSpoiler`, and `isLog`;
maps `seasonId` through `showSeasons[].id` to `seasonNumber`; and displays `backdate` as the
watched-on date. It also uses `dateAdded` for a separate review-ordering view. The frontend does
not establish the API's exact serialized types or nullability, so the ISO dates and numeric IDs below
are illustrative test data, not captured wire values.

`showId` and `showSeasons[].id` values are test identifiers. The app exposes Serializd show IDs in
catalog URLs; the inference that a show ID is a TMDB TV identifier is not asserted by these
fixtures. Diary row IDs identify events. Season IDs use the `serializd_season` namespace and point
to the parent TV work, while the matching season number is stored in `subject_ref`.

| File | Purpose |
|---|---|
| `page-1.json`, `page-2.json` | Two-page diary with ratings, reviews, backdated events, and season/episode references. |
| `empty.json` | Valid empty diary envelope. |
| `page-2-empty.json`, `page-2-total-pages-changed.json`, `page-2-duplicate-id.json` | Pagination integrity failures: an unexpected empty reported page, a changing page count, and a repeated diary event ID. |
| `credentials-invalid.json` | Synthetic 401 response for a rejected bearer token. |
| `rate-limited.json` | Synthetic 429 response; the transport supplies `Retry-After: 120`. |
| `http-error.json` | Synthetic 503 response. |
| `malformed.json` | HTTP 200 with an unexpected JSON envelope. |
| `structure-changed.html` | HTTP 200 HTML in place of the diary JSON. |
| `blocked.html` | Synthetic 403 block page. The provider must stop without attempting a workaround. |
