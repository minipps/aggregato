"""Letterboxd provider: public RSS for automatic recent activity and RSS exports for history.

The public RSS feed is the highest usable automatic surface for recent diary activity.  A supplied
RSS/XML file uses the identical parser for older history, so both paths preserve the same native
GUIDs and normalize into one stable provider id.  A future official export endpoint can therefore
replace only ``_read_feed`` without splitting an archive into a second provider.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any, ClassVar

from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from aggregato.domain.enums import (
    Acquisition,
    Capability,
    Confidence,
    EntryKind,
    ErrorClass,
    FetchMode,
    LoggedPrecision,
    MediaType,
    ReviewFormat,
    ScaleKind,
)
from aggregato.domain.models import (
    Checkpoint,
    CheckResult,
    Cursor,
    NormalizedBatch,
    NormalizedEntry,
    NormalizedExternalId,
    NormalizedOpinion,
    NormalizedWork,
    RawRecord,
)
from aggregato.domain.ratings import RatingScale
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import ProviderError, StructureChangedError

LB = "{https://letterboxd.com}"
TMDB = "{https://themoviedb.org}"


class LetterboxdConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(
        default=None,
        description="Letterboxd username whose public RSS feed should be synchronized.",
    )
    rss_url: HttpUrl | None = Field(
        default=None,
        description="Optional full public RSS URL; overrides the URL derived from username.",
    )
    export_path: Path | None = Field(
        default=None,
        description="Optional local RSS/XML export for a manually requested full sync.",
    )


class LetterboxdProvider:
    id: str = "letterboxd"
    name: str = "Letterboxd"
    media_types: set[MediaType] = {MediaType.FILM, MediaType.TV}  # noqa: RUF012
    capabilities: ClassVar[set[Capability]] = {
        Capability.POLL,
        Capability.FILE_IMPORT,
        Capability.HAS_RATINGS,
        Capability.HAS_REVIEWS,
    }
    acquisition: Acquisition = Acquisition.FEED
    config_model: type[BaseModel] = LetterboxdConfig
    rating_scales: ClassVar[list[RatingScale]] = [
        RatingScale(
            id="letterboxd-5-star-halves",
            kind=ScaleKind.LINEAR,
            min_value=Decimal("0.5"),
            max_value=Decimal(5),
            step=Decimal("0.5"),
        )
    ]
    schema_version: int = 1
    default_poll_interval: timedelta = timedelta(hours=6)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        if cursor is not None and cursor.state.get("feed_complete"):
            yield Checkpoint(cursor=cursor)
            return
        if mode is FetchMode.IMPORT:
            if ctx.import_path is None:
                raise ProviderError("Letterboxd import mode requires an RSS or XML export")
            try:
                payload = ctx.import_path.read_bytes()
            except OSError as exc:
                raise ProviderError(f"cannot read Letterboxd export: {exc}") from exc
        else:
            config = _config(ctx)
            if config.export_path is not None:
                try:
                    payload = config.export_path.read_bytes()
                except OSError as exc:
                    raise ProviderError(f"cannot read Letterboxd export: {exc}") from exc
            else:
                url = _rss_url(config)
                if url is None:
                    return
                response = await ctx.http.get(url)
                if response.status_code != 200:
                    raise ProviderError(f"Letterboxd RSS answered HTTP {response.status_code}")
                payload = response.content
        for item in _items(payload):
            yield RawRecord(native_id=item["guid"], payload=item)
        yield Checkpoint(cursor=Cursor(state={"feed_complete": True}))

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        item = raw.payload
        title = _text(item, "film_title")
        watched = _day(_text(item, "watched_date"))
        rating = _decimal(item.get("member_rating"))
        review = item.get("review_html") or None
        tmdb_id = item.get("tmdb_movie_id") or item.get("tmdb_tv_id")
        ids = [
            NormalizedExternalId(
                namespace="letterboxd", value=raw.native_id, confidence=Confidence.ASSERTED
            )
        ]
        if tmdb_id:
            ids.append(
                NormalizedExternalId(
                    namespace="tmdb", value=str(tmdb_id), confidence=Confidence.ASSERTED
                )
            )
        return NormalizedBatch(
            work=NormalizedWork(
                media_type=MediaType.TV if item.get("tmdb_tv_id") else MediaType.FILM,
                title=title,
                release_year=_integer(item.get("film_year")),
                image_url=item.get("image_url"),
            ),
            entries=[
                NormalizedEntry(
                    kind=EntryKind.REWATCH if item.get("rewatch") == "Yes" else EntryKind.WATCH,
                    logged_at=watched,
                    logged_precision=LoggedPrecision.DAY,
                    native_id=raw.native_id,
                )
            ],
            opinions=[]
            if rating is None and review is None and item.get("member_like") != "Yes"
            else [
                NormalizedOpinion(
                    rating_raw=rating,
                    rating_scale_id="letterboxd-5-star-halves" if rating is not None else None,
                    is_liked=item.get("member_like") == "Yes"
                    if item.get("member_like") is not None
                    else None,
                    review_text=review,
                    review_format=ReviewFormat.HTML if review else None,
                    authored_at=_published(item.get("pub_date")),
                )
            ],
            external_ids=ids,
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        config = _config(ctx)
        if config.export_path is not None:
            try:
                _items(config.export_path.read_bytes())
            except (OSError, ProviderError) as exc:
                return CheckResult(ok=False, error_class=ErrorClass.PARSE, detail=str(exc))
            return CheckResult(ok=True, detail="Letterboxd local RSS export is valid")
        if _rss_url(config) is None:
            return CheckResult(
                ok=False, detail="set username or rss_url to enable automatic Letterboxd RSS sync"
            )
        return CheckResult(ok=True, detail="Letterboxd public RSS is configured")


def _config(ctx: ProviderContext) -> LetterboxdConfig:
    if not isinstance(ctx.config, LetterboxdConfig):
        raise ProviderError("Letterboxd received an invalid configuration model")
    return ctx.config


def _rss_url(config: LetterboxdConfig) -> str | None:
    if config.rss_url is not None:
        return str(config.rss_url)
    return f"https://letterboxd.com/{config.username}/rss/" if config.username else None


def _items(payload: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise StructureChangedError("Letterboxd export is not valid RSS/XML") from exc
    items = root.findall("./channel/item")
    if not items:
        raise StructureChangedError("Letterboxd RSS contains no channel items")
    parsed = [_item(item) for item in items]
    return parsed


def _item(item: ET.Element) -> dict[str, Any]:
    values = {
        "guid": item.findtext("guid"),
        "pub_date": item.findtext("pubDate"),
        "watched_date": item.findtext(f"{LB}watchedDate"),
        "rewatch": item.findtext(f"{LB}rewatch"),
        "film_title": item.findtext(f"{LB}filmTitle"),
        "film_year": item.findtext(f"{LB}filmYear"),
        "member_rating": item.findtext(f"{LB}memberRating"),
        "member_like": item.findtext(f"{LB}memberLike"),
        "tmdb_movie_id": item.findtext(f"{TMDB}movieId"),
        "tmdb_tv_id": item.findtext(f"{TMDB}tvId"),
    }
    if not values["guid"] or not values["watched_date"] or not values["film_title"]:
        raise StructureChangedError(
            "Letterboxd RSS item is missing guid, watchedDate, or filmTitle"
        )
    description = item.findtext("description") or ""
    values["image_url"] = _image(description)
    values["review_html"] = _review(description, values["watched_date"])
    return values


def _image(html: str) -> str | None:
    match = re.search(r'<img[^>]+src=["\']([^"\']+)', html, flags=re.I)
    return unescape(match.group(1)) if match else None


def _review(html: str, watched_date: str) -> str | None:
    text = re.sub(r"<p>\s*<img[^>]*>\s*</p>", "", html, flags=re.I).strip()
    plain = re.sub(r"<[^>]+>", "", text).strip()
    if not plain or plain.startswith("Watched on "):
        return None
    return text


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StructureChangedError(f"Letterboxd item has no {key}")
    return value.strip()


def _day(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise StructureChangedError("Letterboxd watchedDate is not YYYY-MM-DD") from exc


def _published(value: object) -> datetime | None:
    try:
        return parsedate_to_datetime(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _integer(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


provider = LetterboxdProvider()
