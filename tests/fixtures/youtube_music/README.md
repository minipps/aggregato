# YouTube Music browse fixtures

These fixtures are synthetic test inputs, not live recordings. They use the response renderer shape
consumed by `ytmusicapi`'s `get_history` implementation: a `FEmusic_history` browse response with
`singleColumnBrowseResultsRenderer`, `sectionListRenderer`, `musicShelfRenderer`, and
`musicResponsiveListItemRenderer` nodes. No account data or credentials were available to record.

The session cookie in conformance config is a placeholder. The failure bodies and block page are
also synthetic. `page-1.json` covers two day buckets, repeated listens for one video on distinct
days, a duplicate same-day row, a podcast episode that should be skipped, and a deleted song row
without a playable video ID. The duplicate on one day cannot be assigned a separate event time by
the source, so day-precision identity intentionally collapses it during ingest.
Provider item IDs combine each video ID with the resolved local day so normalization replay keeps
separate payloads for the same video on different days.
The credited source uses `MUSIC_PAGE_TYPE_USER_CHANNEL`, which supports channel identity but does
not establish a performer role.
The first row also carries synthetic menu telemetry and a feedback token; fetch must remove these
transient values while keeping its like status and identifiers.

The upstream method and returned history fields are documented in the
[ytmusicapi source](https://github.com/sigma67/ytmusicapi/blob/main/ytmusicapi/mixins/library.py).
