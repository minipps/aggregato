"""Backloggd's public Reviews RSS feed.

The official feed exposes at most the user's latest 20 reviews. It does not expose playthrough
history, so this provider retains review opinions without turning their publication dates into play
entries.
"""

from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, ConfigDict, Field

from aggregato.domain.enums import (
    Acquisition,
    Capability,
    ErrorClass,
    FetchMode,
    MediaType,
    ReviewFormat,
    ScaleKind,
)
from aggregato.domain.models import (
    Checkpoint,
    CheckResult,
    Cursor,
    NormalizedBatch,
    NormalizedOpinion,
    NormalizedWork,
    RawRecord,
)
from aggregato.domain.ratings import RatingScale
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import (
    AuthError,
    BlockedError,
    ProviderError,
    RateLimited,
    StructureChangedError,
    TransportError,
)

FEED_ROOT = "https://backloggd.com"
RATING_TAG = "{https://backloggd.com/}user_rating"
RATING_SCALE = RatingScale(
    id="backloggd-10-point",
    kind=ScaleKind.LINEAR,
    min_value=Decimal(1),
    max_value=Decimal(10),
    step=Decimal(1),
)

_CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "checking your browser",
    "attention required",
    "cf-chl-",
    "challenge-platform",
    "robot check",
)
_SIGN_IN_MARKERS = ("<title>sign in", "<title>log in", "/user/sign_in", "/login")
_REVIEW_ID = re.compile(r"^backloggd-review-[^\s]+$")
_YEAR_AND_STARS = re.compile(r"^(?P<title>.+?)\s+\((?P<year>\d{4})\)(?:\s+-\s+[★☆½ ]+)?$")
_STARS_ONLY = re.compile(r"^(?P<title>.+?)\s+-\s+[★☆½ ]+$")
_HTML_TAG = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
_CHANNEL_ELEMENTS = frozenset(
    {
        "title",
        "link",
        "description",
        "language",
        "copyright",
        "managingEditor",
        "webMaster",
        "pubDate",
        "lastBuildDate",
        "category",
        "generator",
        "docs",
        "cloud",
        "ttl",
        "image",
        "textInput",
        "skipHours",
        "skipDays",
        "item",
    }
)


class BackloggdConfig(BaseModel):
    """Settings for the public Backloggd review feed."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        min_length=1,
        description=(
            "Backloggd username whose public Reviews RSS feed to poll. The feed contains only "
            "the latest 20 reviews; it does not include playthrough history."
        ),
    )


class BackloggdProvider:
    """Poll a Backloggd user's public, rolling Reviews RSS snapshot."""

    id: str = "backloggd"
    name: str = "Backloggd"
    media_types: set[MediaType] = {MediaType.GAME}  # noqa: RUF012
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.HAS_RATINGS,
        Capability.HAS_REVIEWS,
    }
    acquisition: Acquisition = Acquisition.FEED
    config_model: type[BaseModel] = BackloggdConfig
    rating_scales: list[RatingScale] = [RATING_SCALE]  # noqa: RUF012
    schema_version: int = 1
    # Backloggd documents no feed quota. Six hours follows the repository's public-feed cadence
    # while keeping the rolling 20-review window reasonably fresh.
    default_poll_interval: timedelta = timedelta(hours=6)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        """Yield the current rolling review snapshot.

        The feed has no pagination or durable position. Each poll fetches it again, and stable
        review GUIDs let host upserts deduplicate overlapping snapshots.
        """
        del cursor
        if mode not in {FetchMode.INCREMENTAL, FetchMode.FULL}:
            raise ProviderError("Backloggd Reviews RSS does not support file import")

        response = await ctx.http.get(_feed_url(_config(ctx).username))
        _raise_for_response(response.status_code, response.headers, response.content)
        for item in _items(response.content):
            yield RawRecord(native_id=item["guid"], payload=item)

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        """Normalize a retained feed item without inventing a play date or game identifier."""
        item = raw.payload
        title, year = _title_and_year(_required_text(item, "title"))
        guid = _required_text(item, "guid")
        if not _REVIEW_ID.fullmatch(guid):
            raise StructureChangedError("Backloggd review item has an unrecognized GUID")
        if raw.native_id != guid:
            raise ValueError("Backloggd raw record ID does not match its review GUID")

        authored_at = _published_at(item.get("pub_date"))
        description = _optional_text(item.get("description"))
        rating = _rating(item.get("user_rating"))
        opinions: list[NormalizedOpinion] = []
        if rating is not None or description is not None:
            opinions.append(
                NormalizedOpinion(
                    rating_raw=rating,
                    rating_scale_id="backloggd-10-point" if rating is not None else None,
                    review_text=description,
                    review_format=_review_format(description) if description else None,
                    authored_at=authored_at,
                )
            )

        return NormalizedBatch(
            work=NormalizedWork(
                media_type=MediaType.GAME,
                title=title,
                release_year=year,
                image_url=_absolute_image_url(item.get("image_url")),
            ),
            opinions=opinions,
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        """Check public feed access and validate the returned review records."""
        try:
            url = _feed_url(_config(ctx).username)
        except ProviderError as exc:
            return CheckResult(ok=False, error_class=ErrorClass.INTERNAL, detail=str(exc))
        try:
            response = await ctx.http.get(url)
        except (AuthError, BlockedError, RateLimited, StructureChangedError, TransportError) as exc:
            return _check_failure(exc)
        except Exception:
            return CheckResult(
                ok=False,
                error_class=ErrorClass.TRANSPORT,
                detail="Backloggd Reviews RSS request could not be completed; check connectivity.",
            )
        try:
            _raise_for_response(response.status_code, response.headers, response.content)
            for item in _items(response.content):
                self.normalize(RawRecord(native_id=item["guid"], payload=item))
        except (ProviderError, ValueError) as exc:
            return _check_failure(exc)
        return CheckResult(
            ok=True,
            detail="Backloggd Reviews RSS is reachable; it exposes only the latest 20 reviews.",
        )


def _config(ctx: ProviderContext) -> BackloggdConfig:
    if not isinstance(ctx.config, BackloggdConfig):
        raise ProviderError("Backloggd received an invalid configuration model")
    return ctx.config


def _feed_url(username: str) -> str:
    value = username.strip()
    if not value or value in {".", ".."} or any(char in value for char in "/\\?#"):
        raise ProviderError("username must be one Backloggd profile name")
    if any(ord(char) < 32 for char in value):
        raise ProviderError("username must not contain control characters")
    return f"{FEED_ROOT}/u/{quote(value, safe='')}/reviews/rss/"


def _raise_for_response(status: int, headers: Any, body: bytes) -> None:
    text = body.decode("utf-8", errors="replace")
    lowered = text.casefold()
    # Review descriptions can contain any words, including challenge terminology. Inspect only an
    # non-feed response so ordinary user text in a valid RSS item cannot stop a sync.
    if not _looks_like_rss(text):
        if any(marker in lowered for marker in _CHALLENGE_MARKERS):
            raise BlockedError("Backloggd returned an anti-bot challenge; sync stopped")
        if any(marker in lowered for marker in _SIGN_IN_MARKERS):
            raise AuthError("Backloggd requires the configured profile's public review feed")
    if status == 429:
        raise RateLimited(
            "Backloggd rate limited the Reviews RSS request",
            retry_after=_retry_after(headers.get("Retry-After")),
        )
    if status in {401, 403, 404}:
        raise AuthError(
            "Backloggd did not provide a public Reviews RSS feed for the configured username"
        )
    if status >= 500:
        raise TransportError(f"Backloggd Reviews RSS answered HTTP {status}")
    if status != 200:
        raise ProviderError(f"Backloggd Reviews RSS answered HTTP {status}")


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            return None
        seconds = (retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _items(payload: bytes) -> list[dict[str, Any]]:
    """Parse the expected RSS 2.0 envelope and retain each item in a stable raw shape."""
    try:
        root = ET.fromstring(payload)
    except (DefusedXmlException, ET.ParseError) as exc:
        raise StructureChangedError("Backloggd Reviews RSS is not valid XML") from exc
    if _local_name(root.tag) != "rss" or root.attrib.get("version") != "2.0":
        raise StructureChangedError("Backloggd Reviews RSS is not an RSS 2.0 document")
    channel = root.find("channel")
    if channel is None:
        raise StructureChangedError("Backloggd Reviews RSS has no channel")
    for required in ("title", "link", "description"):
        if not _element_text(channel.find(required)):
            raise StructureChangedError(f"Backloggd Reviews RSS channel has no {required}")
    for child in channel:
        local_name = _local_name(child.tag)
        if local_name in {"entry", "review", "reviews"}:
            raise StructureChangedError("Backloggd Reviews RSS channel has an unknown item shape")
        if local_name == "item" and child.tag != "item":
            raise StructureChangedError("Backloggd Reviews RSS item element is namespaced")
        if child.tag.startswith("{") or local_name in _CHANNEL_ELEMENTS:
            continue
        raise StructureChangedError(
            f"Backloggd Reviews RSS channel has an unexpected {local_name} element"
        )

    result = []
    for node in channel.findall("item"):
        guid = _element_text(node.find("guid"))
        title = _element_text(node.find("title"))
        if not guid or not title:
            raise StructureChangedError("Backloggd review item is missing its title or review GUID")
        if not _REVIEW_ID.fullmatch(guid):
            raise StructureChangedError("Backloggd review item has an unrecognized GUID")
        description = _element_text(node.find("description"))
        result.append(
            {
                "title": title,
                "link": _element_text(node.find("link")),
                "pub_date": _element_text(node.find("pubDate")),
                "description": description,
                "guid": guid,
                "user_rating": _element_text(node.find(RATING_TAG)),
                "reviewer": _element_text(node.find("{https://backloggd.com/}reviewer")),
                "image_url": _element_text(node.find("image/url")),
                "source_xml": ET.tostring(node, encoding="unicode"),
            }
        )
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _element_text(element: Any) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = _optional_text(payload.get(key))
    if value is None:
        raise StructureChangedError(f"Backloggd review record has no {key}")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Backloggd review fields must be strings")
    text = value.strip()
    return text or None


def _title_and_year(value: str) -> tuple[str, int | None]:
    """Strip the observed ` (year) - stars` display suffix when it is present."""
    match = _YEAR_AND_STARS.fullmatch(value)
    if match:
        title = match.group("title").strip()
        year = int(match.group("year"))
        return title, year
    stars = _STARS_ONLY.fullmatch(value)
    return (stars.group("title").strip(), None) if stars else (value.strip(), None)


def _rating(value: object) -> Decimal | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        rating = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("Backloggd user_rating is not numeric") from exc
    if not rating.is_finite():
        raise ValueError("Backloggd user_rating must be finite")
    # Backloggd uses zero as the unset rating value in account data.
    if rating == 0:
        return None
    if not RATING_SCALE.admits(rating):
        raise ValueError(f"Backloggd user_rating {rating} is outside the 1–10 point scale")
    return rating


def _published_at(value: object) -> datetime:
    text = _optional_text(value)
    if text is None:
        raise StructureChangedError("Backloggd review item is missing its pubDate")
    try:
        published = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Backloggd pubDate is not a valid RSS date") from exc
    if published.tzinfo is None:
        raise ValueError("Backloggd pubDate must include a timezone")
    return published.astimezone(UTC)


def _review_format(text: str) -> ReviewFormat:
    return ReviewFormat.HTML if _HTML_TAG.search(text) else ReviewFormat.PLAIN


def _absolute_image_url(value: object) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    parsed = urlsplit(text)
    if parsed.scheme == "https" and parsed.netloc:
        return text
    return None


def _looks_like_rss(text: str) -> bool:
    return (
        re.match(r"^\ufeff?\s*(?:<\?xml[^>]*>\s*)?<rss(?:\s|>)", text, flags=re.IGNORECASE)
        is not None
    )


def _check_failure(exc: Exception) -> CheckResult:
    if isinstance(exc, AuthError):
        error_class = ErrorClass.AUTH
    elif isinstance(exc, BlockedError):
        error_class = ErrorClass.BLOCKED
    elif isinstance(exc, RateLimited):
        error_class = ErrorClass.RATE_LIMIT
    elif isinstance(exc, StructureChangedError):
        error_class = ErrorClass.STRUCTURE_CHANGED
    elif isinstance(exc, TransportError):
        error_class = ErrorClass.TRANSPORT
    elif isinstance(exc, ValueError):
        error_class = ErrorClass.PARSE
    else:
        error_class = ErrorClass.INTERNAL
    return CheckResult(ok=False, error_class=error_class, detail=str(exc))


provider = BackloggdProvider()
