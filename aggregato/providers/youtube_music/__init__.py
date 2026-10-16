"""YouTube Music history via its authenticated WEB_REMIX browse endpoint.

YouTube Music does not publish a listening-history API. This provider reads the same authenticated
``FEmusic_history`` Innertube browse surface used by ``ytmusicapi`` and is therefore declared as a
scraper. The response is a bounded rolling snapshot, with sections grouped by day rather than
individual play timestamps. Polling snapshots cannot distinguish repeated plays of one video on
the same day; those records intentionally use Aggregato's event fallback key instead of inventing
an event id.

The browser cookie and account index are copied from an authenticated POST to
``music.youtube.com/youtubei/v1/browse``. A fresh ``SAPISIDHASH`` is generated for each request;
the provider never refreshes or circumvents browser authentication. CAPTCHA and other explicit
anti-bot challenges are reported as blocked.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from aggregato.domain.enums import (
    Acquisition,
    Capability,
    Confidence,
    CreatorKind,
    EntryKind,
    ErrorClass,
    FetchMode,
    LoggedPrecision,
    MediaType,
    Role,
)
from aggregato.domain.models import (
    CheckResult,
    Cursor,
    NormalizedBatch,
    NormalizedCreatorId,
    NormalizedCredit,
    NormalizedEntry,
    NormalizedExternalId,
    NormalizedOpinion,
    NormalizedWork,
    RawRecord,
)
from aggregato.domain.ratings import RatingScale
from aggregato.providers.base import Provider, ProviderContext
from aggregato.providers.errors import (
    AuthError,
    BlockedError,
    ProviderError,
    RateLimited,
    StructureChangedError,
    TransportError,
)

# The browser-authenticated upstream client appends this public WEB_REMIX key to YTM_PARAMS.
# Source: sigma67/ytmusicapi constants.py (YTM_PARAMS_KEY), used only as the public client key.
BASE_URL = (
    "https://music.youtube.com/youtubei/v1/browse?alt=json"
    "&key=AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"
)
ORIGIN = "https://music.youtube.com"
HISTORY_BROWSE_ID = "FEmusic_history"
_PLAYABLE_ARTIST_TYPES = frozenset(
    {"MUSIC_PAGE_TYPE_ARTIST", "MUSIC_PAGE_TYPE_UNKNOWN", "MUSIC_PAGE_TYPE_USER_CHANNEL"}
)
_ALBUM_TYPES = frozenset({"MUSIC_PAGE_TYPE_ALBUM", "MUSIC_PAGE_TYPE_AUDIOBOOK"})
_NON_MUSIC_TYPES = frozenset({"MUSIC_PAGE_TYPE_NON_MUSIC_AUDIO_TRACK_PAGE"})
_NON_MUSIC_VIDEO_TYPES = frozenset({"MUSIC_VIDEO_TYPE_PODCAST_EPISODE"})
_LIKE_STATUSES = frozenset({"LIKE", "DISLIKE", "INDIFFERENT"})
_TRANSIENT_HISTORY_KEYS = frozenset(
    {"feedbacktoken", "feedbacktokens", "servicetrackingparams", "trackingparams"}
)
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_DATE_FORMATS = (
    "%A, %B %d, %Y",
    "%a, %b %d, %Y",
    "%A, %b %d",
    "%a, %B %d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%Y-%m-%d",
    "%B %d",
    "%b %d",
    "%m/%d/%Y",
    "%m/%d",
)
_CHALLENGE_MARKERS = (
    "captcha",
    "unusual traffic",
    "confirm you're not a bot",
    "confirm you are not a bot",
    "not a robot",
    "automated requests",
    "security challenge",
)
_AUTH_MARKERS = ("sign in to", "please sign in", "login_required", "authentication required")


class YouTubeMusicConfig(BaseModel):
    """Browser credentials and the local timezone used by YouTube Music history sections."""

    model_config = ConfigDict(extra="forbid")

    cookie: SecretStr = Field(
        min_length=1,
        description=(
            "Full Cookie request-header value from an authenticated POST to "
            "music.youtube.com/browse. The provider needs the __Secure-3PAPISID cookie to "
            "authorize history requests. Use a ${ENV_VAR} reference to keep the browser session "
            "out of the config file."
        ),
    )
    auth_user: str = Field(
        default="0",
        min_length=1,
        description="Value of X-Goog-AuthUser from the same authenticated browser request.",
    )
    timezone: str = Field(
        default="UTC",
        min_length=1,
        description=(
            "IANA time zone used by YouTube Music to group history days, for example "
            "Europe/Madrid. This anchors Today, Yesterday, and weekday labels; the default is UTC."
        ),
    )

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"timezone must be an installed IANA timezone, got {value!r}") from exc
        return normalized


class YouTubeMusicProvider:
    """Authenticated rolling-snapshot provider for YouTube Music play history."""

    id: str = "youtube_music"
    name: str = "YouTube Music"
    media_types: set[MediaType] = {MediaType.TRACK}  # noqa: RUF012
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.SCRAPES,
        Capability.HAS_CREDITS,
    }
    acquisition: Acquisition = Acquisition.SCRAPE
    config_model: type[BaseModel] = YouTubeMusicConfig
    rating_scales: list[RatingScale] = []  # noqa: RUF012
    schema_version: int = 1
    default_poll_interval: timedelta = timedelta(minutes=15)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord]:
        """Read one current history snapshot; this endpoint has no backfill cursor."""
        del cursor  # The endpoint returns a rolling snapshot and exposes no stable continuation.
        if mode is FetchMode.IMPORT:
            raise ProviderError("YouTube Music does not support file import")
        if mode not in {FetchMode.INCREMENTAL, FetchMode.FULL}:
            raise ProviderError(f"YouTube Music does not support fetch mode {mode.value!r}")

        config = _config(ctx)
        body, anchor = await _history(ctx, config)
        for row, label, logged_at, parsed in _history_records(body, anchor, config.timezone):
            local_day = logged_at.astimezone(ZoneInfo(config.timezone)).date().isoformat()
            payload = {
                "item": _sanitize_history_item(row),
                "played_label": label,
                "logged_at": logged_at.isoformat(),
                "logged_day": local_day,
                "timezone": config.timezone,
            }
            # Distinct day buckets must retain distinct raw payloads for replay, while same-day
            # plays share a key because the source exposes neither event ids nor exact times.
            yield RawRecord(native_id=_raw_item_id(parsed.video_id, local_day), payload=payload)

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        """Purely map a stored renderer and captured day anchor to one track listen."""
        payload = raw.payload
        item = payload.get("item")
        if not isinstance(item, dict):
            raise StructureChangedError("YouTube Music stored history payload has no item object")
        renderer = _history_renderer(item)
        if _is_non_music(renderer):
            raise StructureChangedError("YouTube Music stored a non-music row as a history record")
        parsed = _parse_item(renderer)
        if parsed.deleted:
            raise StructureChangedError("YouTube Music deleted rows must not be normalized")
        logged_at = _aware_datetime(payload.get("logged_at"))
        logged_day = _iso_day(payload.get("logged_day"))
        timezone = _required_text(payload.get("timezone"), "timezone")
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise StructureChangedError(
                "YouTube Music history payload has an invalid timezone"
            ) from exc
        expected_logged_at = datetime.combine(logged_day, day_time.min, tzinfo=zone).astimezone(UTC)
        if logged_at != expected_logged_at:
            raise StructureChangedError(
                "YouTube Music logged_at is inconsistent with its captured local day and timezone"
            )
        if raw.native_id != _raw_item_id(parsed.video_id, logged_day.isoformat()):
            raise StructureChangedError("YouTube Music raw item id does not match its video day")

        entry = NormalizedEntry(
            kind=EntryKind.LISTEN,
            logged_at=logged_at,
            logged_precision=LoggedPrecision.DAY,
            native_id=None,
            metadata={"played_label": _required_text(payload.get("played_label"), "played_label")},
        )
        external_ids = [
            NormalizedExternalId(
                namespace="youtube_video",
                value=parsed.video_id,
                confidence=Confidence.ASSERTED,
            )
        ]
        creator_ids: list[NormalizedCreatorId] = []
        credits: list[NormalizedCredit] = []
        known_creator_ids: set[str] = set()
        for position, artist in enumerate(parsed.artists):
            credits.append(
                NormalizedCredit(
                    creator_name=artist.name,
                    creator_kind=CreatorKind.UNKNOWN,
                    role=(
                        Role.PERFORMER
                        if artist.page_type == "MUSIC_PAGE_TYPE_ARTIST"
                        else Role.OTHER
                    ),
                    role_raw=artist.page_type,
                    position=position,
                )
            )
            if artist.browse_id:
                known_creator_ids.add(artist.browse_id)
                creator_ids.append(
                    NormalizedCreatorId(
                        creator_name=artist.name,
                        namespace="youtube_channel",
                        value=artist.browse_id,
                        confidence=Confidence.ASSERTED,
                    )
                )

        for path, identifier in _identifier_values(item):
            key = path[-1].casefold()
            if key == "videoid" and identifier == parsed.video_id:
                continue  # Represent the work's video id once under its canonical namespace.
            if key == "browseid" and identifier in known_creator_ids:
                continue  # The same artist/channel id is already correctly creator-scoped.
            if key == "browseid" and identifier in parsed.album_ids:
                continue  # Album ids get the more explicit association below.
            scoped_value = f"video:{parsed.video_id}:source:{'.'.join(path)}:{identifier}"
            external_ids.append(
                NormalizedExternalId(
                    namespace="youtube_music",
                    value=scoped_value,
                    confidence=Confidence.ASSERTED,
                )
            )

        for album_id in parsed.album_ids:
            external_ids.append(
                NormalizedExternalId(
                    namespace="youtube_music",
                    value=f"video:{parsed.video_id}:album:{album_id}",
                    confidence=Confidence.ASSERTED,
                )
            )

        metadata: dict[str, Any] = {}
        if parsed.album_names:
            metadata["album_titles"] = parsed.album_names
        if parsed.video_types:
            metadata["video_types"] = parsed.video_types
        if parsed.like_status:
            metadata["like_status"] = parsed.like_status

        opinions = []
        if parsed.like_status in {"LIKE", "DISLIKE"}:
            opinions.append(NormalizedOpinion(is_liked=parsed.like_status == "LIKE"))

        return NormalizedBatch(
            work=NormalizedWork(
                media_type=MediaType.TRACK,
                title=parsed.title,
                image_url=parsed.image_url,
                metadata=metadata,
            ),
            entries=[entry],
            opinions=opinions,
            credits=credits,
            external_ids=_unique_external_ids(external_ids),
            creator_external_ids=_unique_creator_ids(creator_ids),
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        """Verify browser authentication, history structure, and timezone interpretation."""
        try:
            config = _config(ctx)
            body, anchor = await _history(ctx, config)
            count = sum(1 for _ in _history_records(body, anchor, config.timezone))
        except ProviderError as exc:
            return CheckResult(ok=False, error_class=exc.error_class, detail=str(exc))
        except Exception as exc:
            return CheckResult(
                ok=False,
                error_class=ErrorClass.INTERNAL,
                detail=f"YouTube Music history check failed ({type(exc).__name__})",
            )
        return CheckResult(
            ok=True,
            detail=(
                f"Authenticated YouTube Music history access succeeded; the current rolling "
                f"snapshot contains {count} music item(s). This does not backfill older history."
            ),
        )


provider: Provider = YouTubeMusicProvider()


async def _history(
    ctx: ProviderContext, config: YouTubeMusicConfig
) -> tuple[dict[str, Any], datetime]:
    """Fetch and validate the history response and its server-supplied date anchor."""
    try:
        response = await ctx.http.post(
            BASE_URL,
            json=_request_body(),
            headers=_request_headers(config),
        )
    except ProviderError:
        raise
    except Exception as exc:
        raise TransportError(f"YouTube Music history request failed: {type(exc).__name__}") from exc

    _raise_for_response(response)
    try:
        body = response.json()
    except (ValueError, TypeError) as exc:
        text = _response_text(response)
        if _contains_marker(text, _CHALLENGE_MARKERS):
            raise BlockedError(
                "YouTube Music returned an anti-bot challenge; wait and retry manually later"
            ) from exc
        if _contains_marker(text, _AUTH_MARKERS):
            raise AuthError(
                "YouTube Music returned a sign-in page; refresh the browser cookie and "
                "X-Goog-AuthUser"
            ) from exc
        raise StructureChangedError("YouTube Music history response is not valid JSON") from exc
    if not isinstance(body, dict):
        raise StructureChangedError("YouTube Music history response is not a JSON object")

    response_text = " ".join(
        _json_strings(body.get(key))
        for key in ("error", "errorMessage", "errorDetails", "playabilityStatus", "alerts")
        if key in body
    )
    if _contains_marker(response_text, _CHALLENGE_MARKERS):
        raise BlockedError(
            "YouTube Music returned an anti-bot challenge; wait and retry manually later"
        )
    if _contains_marker(response_text, _AUTH_MARKERS):
        raise AuthError(
            "YouTube Music requires sign-in; refresh the browser cookie and X-Goog-AuthUser"
        )
    anchor = _http_date(response.headers.get("Date"))
    return body, anchor


def _config(ctx: ProviderContext) -> YouTubeMusicConfig:
    if not isinstance(ctx.config, YouTubeMusicConfig):
        raise ProviderError("YouTube Music provider requires YouTubeMusicConfig")
    return ctx.config


def _request_body() -> dict[str, Any]:
    client_version = datetime.now(UTC).strftime("1.%Y%m%d.01.00")
    return {
        "context": {
            "client": {
                "clientName": "WEB_REMIX",
                "clientVersion": client_version,
                "hl": "en",
            },
            "user": {},
        },
        "browseId": HISTORY_BROWSE_ID,
    }


def _request_headers(config: YouTubeMusicConfig) -> dict[str, str]:
    cookie = config.cookie.get_secret_value()
    sapisid = _cookie_value(cookie, "__Secure-3PAPISID")
    if sapisid is None:
        raise AuthError(
            "YouTube Music cookie has no __Secure-3PAPISID; copy the full Cookie header "
            "from an authenticated music.youtube.com browse request"
        )
    timestamp = str(int(time.time()))
    # SHA-1 is mandated by Google's legacy SAPISIDHASH authorization protocol.
    digest = hashlib.sha1(  # noqa: S324
        f"{timestamp} {sapisid} {ORIGIN}".encode()
    ).hexdigest()
    return {
        "Authorization": f"SAPISIDHASH {timestamp}_{digest}",
        "X-Goog-AuthUser": config.auth_user,
        "Cookie": cookie,
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Accept": "*/*",
    }


def _cookie_value(cookie_header: str, name: str) -> str | None:
    for item in cookie_header.split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key == name and value:
            return value.strip().strip('"') or None
    return None


def _raise_for_response(response: Any) -> None:
    status = response.status_code
    if status != 200:
        response_text = _response_text(response)
        if _contains_marker(response_text, _CHALLENGE_MARKERS):
            raise BlockedError(
                "YouTube Music returned an anti-bot challenge; wait and retry manually later"
            )
    if status == 401:
        raise AuthError(
            "YouTube Music rejected browser authentication; refresh the cookie and X-Goog-AuthUser"
        )
    if status == 403:
        raise AuthError(
            "YouTube Music refused the browser credentials (HTTP 403); verify the copied cookie "
            "and account index"
        )
    if status == 429:
        raise RateLimited(
            "YouTube Music is rate-limiting the history request (HTTP 429)",
            retry_after=_retry_after(response.headers.get("Retry-After")),
        )
    if 500 <= status <= 599:
        raise TransportError(f"YouTube Music history endpoint returned HTTP {status}")
    if status != 200:
        raise StructureChangedError(f"YouTube Music history endpoint returned HTTP {status}")


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _history_sections(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        contents = body["contents"]["singleColumnBrowseResultsRenderer"]
        tabs = contents["tabs"]
        tab = tabs[0]["tabRenderer"]
        section_list = tab["content"]["sectionListRenderer"]
        sections = section_list["contents"]
    except (KeyError, IndexError, TypeError) as exc:
        raise StructureChangedError(
            "YouTube Music history response has no single-column section list"
        ) from exc
    if not isinstance(tabs, list) or not tabs:
        raise StructureChangedError("YouTube Music history response has no tab")
    if not isinstance(sections, list):
        raise StructureChangedError("YouTube Music history section contents is not a list")
    normalized: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            raise StructureChangedError("YouTube Music history contains a non-object section")
        if "musicNotifierShelfRenderer" in section:
            notifier = section["musicNotifierShelfRenderer"]
            if not isinstance(notifier, dict):
                raise StructureChangedError("YouTube Music history notifier shelf is not an object")
            note = _text_value(notifier.get("title"))
            if note and _contains_marker(note, _CHALLENGE_MARKERS):
                raise BlockedError(
                    "YouTube Music returned an anti-bot challenge; wait and retry manually later"
                )
            if note and _contains_marker(note, _AUTH_MARKERS):
                raise AuthError(
                    "YouTube Music requires sign-in; refresh the browser cookie and X-Goog-AuthUser"
                )
            raise StructureChangedError(
                "YouTube Music history returned a notifier shelf instead of history items"
            )
        if "musicShelfRenderer" not in section:
            raise StructureChangedError("YouTube Music history section has no musicShelfRenderer")
        normalized.append(section)
    return normalized


def _history_records(
    body: Mapping[str, Any], anchor: datetime, timezone: str
) -> Iterator[tuple[dict[str, Any], str, datetime, _ParsedItem]]:
    """Validate sections once and yield only retained music rows with their captured day."""
    zone = ZoneInfo(timezone)
    for section in _history_sections(body):
        shelf = section["musicShelfRenderer"]
        if not isinstance(shelf, dict):
            raise StructureChangedError("YouTube Music history shelf is not an object")
        label = _text_value(shelf.get("title"))
        if not label:
            raise StructureChangedError("YouTube Music history shelf has no day label")
        logged_at = _day_start(label, anchor, zone)
        rows = shelf.get("contents")
        if not isinstance(rows, list):
            raise StructureChangedError("YouTube Music history shelf has no contents list")
        for row in rows:
            renderer = _history_renderer(row)
            if _is_non_music(renderer):
                continue
            parsed = _parse_item(renderer)
            if not parsed.deleted:
                yield row, label, logged_at, parsed


def _history_renderer(row: object) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise StructureChangedError("YouTube Music history row is not an object")
    renderer = row.get("musicResponsiveListItemRenderer")
    if not isinstance(renderer, dict):
        raise StructureChangedError(
            "YouTube Music history row has no musicResponsiveListItemRenderer"
        )
    return renderer


def _sanitize_history_item(value: Any) -> Any:
    """Copy source data while dropping action tokens and request telemetry before retention."""
    if isinstance(value, dict):
        return {
            key: _sanitize_history_item(child)
            for key, child in value.items()
            if str(key).casefold() not in _TRANSIENT_HISTORY_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_history_item(child) for child in value]
    return value


def _day_start(label: str, anchor: datetime, zone: ZoneInfo) -> datetime:
    """Resolve a source grouping label to the start of that local calendar day."""
    normalized = " ".join(label.strip().split())
    folded = normalized.casefold()
    local_anchor = anchor.astimezone(zone)
    day = local_anchor.date()
    if folded == "today":
        target = day
    elif folded == "yesterday":
        target = day - timedelta(days=1)
    elif folded in _WEEKDAYS:
        elapsed = (day.weekday() - _WEEKDAYS[folded]) % 7
        target = day - timedelta(days=elapsed or 7)
    else:
        target = _parse_absolute_day(normalized, day)
    return datetime.combine(target, day_time.min, tzinfo=zone).astimezone(UTC)


def _parse_absolute_day(label: str, anchor_day: date) -> date:
    for pattern in _DATE_FORMATS:
        includes_year = any(token in pattern for token in ("%Y",))
        if includes_year:
            try:
                parsed = datetime.strptime(  # noqa: DTZ007 -- source label is a date
                    label, pattern
                ).date()
            except ValueError:
                continue
            if parsed > anchor_day:
                raise StructureChangedError(
                    f"YouTube Music history date {label!r} is later than the response Date anchor"
                )
            return parsed

        # Try candidate years directly instead of parsing with datetime's implicit year 1900.
        # Besides fixing leap-day labels, this moves ambiguous month/day labels to the nearest
        # source date at or before the response anchor without inventing a future year.
        parsed = None
        for year in range(anchor_day.year, anchor_day.year - 9, -1):
            try:
                candidate = datetime.strptime(  # noqa: DTZ007 -- source label is a date
                    f"{label} {year}", f"{pattern} %Y"
                ).date()
            except ValueError:
                continue
            if candidate <= anchor_day:
                parsed = candidate
                break
        if parsed is not None:
            return parsed
    raise StructureChangedError(
        f"YouTube Music history day label {label!r} is not a supported English date"
    )


def _http_date(value: str | None) -> datetime:
    if not value:
        raise StructureChangedError(
            "YouTube Music response has no HTTP Date header to anchor relative history labels"
        )
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StructureChangedError(
            "YouTube Music response has an invalid HTTP Date header"
        ) from exc
    if parsed.tzinfo is None:
        raise StructureChangedError("YouTube Music response HTTP Date header has no timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _Artist:
    name: str
    browse_id: str | None
    page_type: str


@dataclass(frozen=True, slots=True)
class _ParsedItem:
    title: str
    video_id: str
    artists: list[_Artist]
    album_names: list[str]
    album_ids: list[str]
    video_types: list[str]
    image_url: str | None
    deleted: bool = False
    like_status: str | None = None


def _parse_item(renderer: Mapping[str, Any]) -> _ParsedItem:
    columns = renderer.get("flexColumns")
    if not isinstance(columns, list) or not columns:
        raise StructureChangedError("YouTube Music history item has no flexColumns list")

    title: str | None = None
    artists: list[_Artist] = []
    album_names: list[str] = []
    album_ids: list[str] = []
    fallback_credit: str | None = None
    for index, column in enumerate(columns):
        runs = _column_runs(column)
        has_endpoint = False
        for run in runs:
            text = _text_value(run.get("text"))
            endpoint = run.get("navigationEndpoint")
            if isinstance(endpoint, dict):
                has_endpoint = True
            browse = _browse_endpoint(endpoint)
            watch = _watch_endpoint(endpoint)
            if watch and text:
                title = text
            if browse is None:
                continue
            browse_id, page_type = browse
            if page_type in _PLAYABLE_ARTIST_TYPES and text:
                artist = _Artist(text, browse_id, page_type)
                if not any(
                    a.name == artist.name and a.browse_id == artist.browse_id for a in artists
                ):
                    artists.append(artist)
            elif page_type in _ALBUM_TYPES:
                if text and text not in album_names:
                    album_names.append(text)
                if browse_id and browse_id not in album_ids:
                    album_ids.append(browse_id)
        column_text = _column_text(column)
        if (
            index > 0
            and not has_endpoint
            and column_text
            and not _is_duration(column_text)
            and fallback_credit is None
        ):
            # ytmusicapi treats the first unrecognized non-duration flex column as artist text.
            # Keep the source text but do not invent an artist id or performer role.
            fallback_credit = column_text

    # Unavailable/deleted source rows may not carry navigation endpoints; their first column still
    # contains the source's displayed title. This mirrors the field position used by ytmusicapi.
    if title is None:
        title = _column_text(columns[0])
    if title == "Song deleted":
        return _ParsedItem(
            title=title,
            video_id="",
            artists=[],
            album_names=[],
            album_ids=[],
            video_types=[],
            image_url=None,
            deleted=True,
        )
    if not title:
        raise StructureChangedError("YouTube Music history item has no title")

    video_id = _video_id(renderer)
    if not video_id:
        raise StructureChangedError(f"YouTube Music history item {title!r} has no video id")
    video_types = _video_types(renderer)
    image_url = _thumbnail_url(renderer)
    if fallback_credit and not artists:
        artists.append(_Artist(fallback_credit, None, "text"))
    return _ParsedItem(
        title=title,
        video_id=video_id,
        artists=artists,
        album_names=album_names,
        album_ids=album_ids,
        video_types=video_types,
        image_url=image_url,
        like_status=_like_status(renderer),
    )


def _is_duration(value: str) -> bool:
    return bool(re.fullmatch(r"\d+(?::\d{2}){1,2}", value.strip()))


def _column_runs(column: object) -> list[dict[str, Any]]:
    if not isinstance(column, dict):
        raise StructureChangedError("YouTube Music history flex column is not an object")
    flex = column.get("musicResponsiveListItemFlexColumnRenderer", column)
    if not isinstance(flex, dict):
        raise StructureChangedError("YouTube Music history flex column renderer is not an object")
    text = flex.get("text")
    if not isinstance(text, dict):
        return []
    runs = text.get("runs")
    if isinstance(runs, list):
        if not all(isinstance(run, dict) for run in runs):
            raise StructureChangedError("YouTube Music history text runs contain a non-object")
        return runs
    if isinstance(text.get("simpleText"), str):
        return [{"text": text["simpleText"]}]
    return []


def _column_text(column: object) -> str | None:
    return (
        " ".join(text for run in _column_runs(column) if (text := _text_value(run.get("text"))))
        or None
    )


def _browse_endpoint(endpoint: object) -> tuple[str | None, str] | None:
    if not isinstance(endpoint, dict):
        return None
    browse_endpoint = endpoint.get("browseEndpoint")
    if not isinstance(browse_endpoint, dict):
        return None
    browse_id = _text_value(browse_endpoint.get("browseId"))
    configs = browse_endpoint.get("browseEndpointContextSupportedConfigs")
    config = configs.get("browseEndpointContextMusicConfig") if isinstance(configs, dict) else None
    page_type = _text_value(config.get("pageType")) if isinstance(config, dict) else None
    return (browse_id, page_type or "")


def _watch_endpoint(endpoint: object) -> bool:
    return (
        isinstance(endpoint, dict)
        and isinstance(endpoint.get("watchEndpoint"), dict)
        and bool(_text_value(endpoint["watchEndpoint"].get("videoId")))
    )


def _video_id(renderer: Mapping[str, Any]) -> str | None:
    # The play button is the canonical playable id in the upstream playlist parser. The title
    # endpoint and removedVideoId support unavailable renderers where no play button is present.
    for node in _walk(renderer):
        if not isinstance(node, dict):
            continue
        value = _text_value(node.get("videoId"))
        if value:
            return value
        removed = node.get("removedVideoId")
        if isinstance(removed, str) and removed.strip():
            return removed.strip()
    return None


def _video_types(renderer: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for node in _walk(renderer):
        if not isinstance(node, dict):
            continue
        endpoint = node.get("watchEndpoint")
        if isinstance(endpoint, dict):
            video_type = _text_value(endpoint.get("videoType"))
            if video_type and video_type not in found:
                found.append(video_type)
            music_supported = endpoint.get("watchEndpointMusicSupportedConfigs")
            music_config = (
                music_supported.get("watchEndpointMusicConfig")
                if isinstance(music_supported, dict)
                else None
            )
            music_video_type = (
                _text_value(music_config.get("musicVideoType"))
                if isinstance(music_config, dict)
                else None
            )
            if music_video_type and music_video_type not in found:
                found.append(music_video_type)
        video_type = _text_value(node.get("videoType"))
        if video_type and video_type not in found:
            found.append(video_type)
    return found


def _like_status(renderer: Mapping[str, Any]) -> str | None:
    for node in _walk(renderer):
        if not isinstance(node, Mapping):
            continue
        value = _text_value(node.get("likeStatus"))
        if value and value in _LIKE_STATUSES:
            return value
    return None


def _is_non_music(renderer: Mapping[str, Any]) -> bool:
    if any(video_type in _NON_MUSIC_VIDEO_TYPES for video_type in _video_types(renderer)):
        return True
    columns = renderer.get("flexColumns", [])
    if not isinstance(columns, list):
        return False
    for column in columns:
        for run in _column_runs(column):
            endpoint = run.get("navigationEndpoint")
            browse = _browse_endpoint(endpoint)
            if browse is not None and browse[1] in _NON_MUSIC_TYPES:
                return True
    return False


def _thumbnail_url(renderer: Mapping[str, Any]) -> str | None:
    for node in _walk(renderer):
        if not isinstance(node, dict):
            continue
        thumbnail = node.get("thumbnail")
        if not isinstance(thumbnail, dict):
            continue
        for nested in _walk(thumbnail):
            if isinstance(nested, dict):
                url = _text_value(nested.get("url"))
                if url:
                    return url
    return None


def _identifier_values(
    node: object, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], str]]:
    found: list[tuple[tuple[str, ...], str]] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            key_text = str(key)
            child_path = (*path, key_text)
            normalized_key = key_text.casefold()
            is_identifier = normalized_key.endswith("id") or normalized_key.endswith("ids")
            if is_identifier and isinstance(value, str | int) and not isinstance(value, bool):
                text = str(value).strip()
                if text:
                    found.append((child_path, text))
            else:
                found.extend(_identifier_values(value, child_path))
    elif isinstance(node, list):
        for item in node:
            found.extend(_identifier_values(item, path))
    return found


def _unique_external_ids(
    values: Sequence[NormalizedExternalId],
) -> list[NormalizedExternalId]:
    result: list[NormalizedExternalId] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        key = (value.namespace, value.value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _unique_creator_ids(
    values: Sequence[NormalizedCreatorId],
) -> list[NormalizedCreatorId]:
    result: list[NormalizedCreatorId] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        key = (value.namespace, value.value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _aware_datetime(value: object) -> datetime:
    text = _required_text(value, "logged_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StructureChangedError(
            "YouTube Music history payload has an invalid logged_at"
        ) from exc
    if parsed.tzinfo is None:
        raise StructureChangedError("YouTube Music history logged_at has no timezone")
    return parsed.astimezone(UTC)


def _iso_day(value: object) -> date:
    text = _required_text(value, "logged_day")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise StructureChangedError(
            "YouTube Music history payload has an invalid logged_day"
        ) from exc
    if parsed.isoformat() != text:
        raise StructureChangedError("YouTube Music logged_day must use YYYY-MM-DD format")
    return parsed


def _raw_item_id(video_id: str, local_day: str) -> str:
    return f"{video_id}:{local_day}"


def _required_text(value: object, label: str) -> str:
    text = _text_value(value)
    if text is None:
        raise StructureChangedError(f"YouTube Music history payload has no {label}")
    return text


def _text_value(value: object) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, dict):
        simple = value.get("simpleText")
        if isinstance(simple, str) and simple.strip():
            return simple.strip()
        runs = value.get("runs")
        if isinstance(runs, list):
            pieces = [run.get("text", "") for run in runs if isinstance(run, dict)]
            joined = "".join(piece for piece in pieces if isinstance(piece, str)).strip()
            return joined or None
    return None


def _walk(value: object) -> Iterator[object]:
    """Yield nested mappings/lists without changing their order."""
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _json_strings(value: object, *, limit: int = 20_000) -> str:
    """Collect a bounded prefix of response strings for sign-in/challenge classification."""
    chunks: list[str] = []
    length = 0
    for item in _walk(value):
        if not isinstance(item, Mapping):
            continue
        for child in item.values():
            if isinstance(child, str):
                if length >= limit:
                    return " ".join(chunks)
                clipped = child[: limit - length]
                chunks.append(clipped)
                length += len(clipped)
    return " ".join(chunks)


def _response_text(response: Any) -> str:
    try:
        return str(response.text)[:20_000]
    except Exception:
        return ""


def _contains_marker(value: str, markers: Sequence[str]) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in markers)
