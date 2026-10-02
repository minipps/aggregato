"""Goodreads bookshelf RSS provider."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

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
    ReviewFormat,
    Role,
    ScaleKind,
)
from aggregato.domain.models import (
    Checkpoint,
    CheckResult,
    Cursor,
    NormalizedBatch,
    NormalizedCredit,
    NormalizedEntry,
    NormalizedExternalId,
    NormalizedOpinion,
    NormalizedWork,
    RawRecord,
)
from aggregato.domain.ratings import RatingScale
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import AuthError, BlockedError, ProviderError, StructureChangedError

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

GOODREADS_HOSTS = frozenset({"goodreads.com", "www.goodreads.com"})
REVIEW_ID_RE = re.compile(r"/review/show/(\d+)(?:[/?#]|$)")
RATING_RE = re.compile(r"\b([0-5])(?:\.0)?\b")


class GoodreadsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_url: HttpUrl | None = Field(
        default=None,
        description=(
            "Public Goodreads profile URL (/user/show/<id>) or RSS URL "
            "(/review/list_rss/<id>). The public RSS feed is refreshed daily."
        ),
    )

    @field_validator("profile_url", mode="before")
    @classmethod
    def _empty_optional_setting_is_unset(cls, value: object) -> object:
        """Keep an unfilled optional form field from becoming a bad URL."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("profile_url")
    @classmethod
    def _goodreads_profile(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.host not in GOODREADS_HOSTS:
            raise ValueError("profile_url must point to goodreads.com")
        return value


class GoodreadsProvider:
    id: str = "goodreads"
    name: str = "Goodreads"
    media_types: set[MediaType] = {MediaType.BOOK}  # noqa: RUF012
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.HAS_RATINGS,
        Capability.HAS_REVIEWS,
        Capability.HAS_CREDITS,
    }
    acquisition: Acquisition = Acquisition.FEED
    config_model: type[BaseModel] = GoodreadsConfig
    rating_scales: list[RatingScale] = [  # noqa: RUF012
        RatingScale(
            id="goodreads-5-star",
            kind=ScaleKind.LINEAR,
            min_value=Decimal(1),
            max_value=Decimal(5),
            step=Decimal(1),
        )
    ]
    schema_version: int = 3
    default_poll_interval: timedelta = timedelta(days=1)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        config = _config(ctx)
        if config.profile_url is None:
            raise ProviderError("set profile_url for Goodreads bookshelf RSS sync")

        # Like Letterboxd RSS, Goodreads RSS is a changing snapshot. Ignore a previous CSV import
        # cursor and refetch it every time; writes deduplicate on the Goodreads book id.
        response = await ctx.http.get(_rss_url(config.profile_url))
        if response.status_code == 401:
            raise AuthError("Goodreads rejected access to the bookshelf RSS feed")
        if response.status_code != 200:
            raise ProviderError(f"Goodreads bookshelf RSS answered HTTP {response.status_code}")
        for item in _items(response.content):
            yield RawRecord(native_id=item["Book Id"], payload=item)
        yield Checkpoint(cursor=Cursor(state={}))

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        row = raw.payload
        title = _required(row, "Title")
        author = _required(row, "Author")
        year = _year(row.get("Year Published")) or _year(row.get("Original Publication Year"))
        # Date Added records library management, not a reading event.
        read_at = _date(row.get("Date Read"))
        ids = [
            NormalizedExternalId(
                namespace="goodreads", value=raw.native_id, confidence=Confidence.ASSERTED
            )
        ]
        for column, namespace in (
            ("ISBN", "isbn"),
            ("ISBN13", "isbn13"),
            ("Review Id", "goodreads_review"),
            ("Work Id", "goodreads_work"),
        ):
            if value := _identifier(row.get(column), isbn=namespace.startswith("isbn")):
                ids.append(
                    NormalizedExternalId(
                        namespace=namespace, value=value, confidence=Confidence.ASSERTED
                    )
                )
        rating = _rating(row.get("My Rating"))
        review = (row.get("My Review") or "").strip() or None
        return NormalizedBatch(
            work=NormalizedWork(
                media_type=MediaType.BOOK,
                title=title,
                release_year=year,
                image_url=row.get("Image URL") or None,
                metadata={
                    "binding": row.get("Binding") or None,
                    "publisher": row.get("Publisher") or None,
                    "pages": row.get("Number of Pages") or None,
                    "date_added": row.get("Date Added") or None,
                    "bookshelves": row.get("Bookshelves") or None,
                    "exclusive_shelf": row.get("Exclusive Shelf") or None,
                },
            ),
            entries=[
                NormalizedEntry(
                    kind=EntryKind.READ,
                    logged_at=read_at,
                    logged_precision=LoggedPrecision.DAY,
                    native_id=raw.native_id,
                )
            ]
            if read_at
            else [],
            opinions=[]
            if rating is None and review is None
            else [
                NormalizedOpinion(
                    rating_raw=rating,
                    rating_scale_id="goodreads-5-star" if rating is not None else None,
                    review_text=review,
                    review_format=ReviewFormat.PLAIN if review else None,
                    contains_spoilers=_yes(row.get("Spoiler")),
                    authored_at=read_at,
                )
            ],
            credits=[
                NormalizedCredit(
                    creator_name=author,
                    creator_kind=CreatorKind.PERSON,
                    role=Role.AUTHOR,
                    role_raw="Author",
                    position=0,
                )
            ],
            external_ids=ids,
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        config = _config(ctx)
        if config.profile_url is not None:
            return CheckResult(ok=True, detail="Goodreads bookshelf RSS is configured")
        return CheckResult(
            ok=False,
            error_class=ErrorClass.AUTH,
            detail="set profile_url to enable Goodreads bookshelf RSS sync",
        )


def _config(ctx: ProviderContext) -> GoodreadsConfig:
    if not isinstance(ctx.config, GoodreadsConfig):
        raise ProviderError("Goodreads received an invalid configuration model")
    return ctx.config


def _rss_url(profile_url: HttpUrl) -> str:
    parsed = urlsplit(str(profile_url))
    match = re.fullmatch(r"/user/show/(\d+(?:-[^/]*)?)/?", parsed.path)
    path = f"/review/list_rss/{match.group(1)}" if match else parsed.path.rstrip("/")
    if not re.fullmatch(r"/review/list_rss/\d+(?:-[^/]*)?", path):
        raise ProviderError(
            "profile_url must be a Goodreads profile or /review/list_rss/<user id> URL"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _items(payload: bytes) -> list[dict[str, str]]:
    """Map Goodreads' RSS extension elements onto the durable CSV-like raw shape."""
    text = payload.decode("utf-8", errors="replace")
    lowered = text.casefold()
    if "captcha" in lowered or "robot check" in lowered:
        raise BlockedError("Goodreads returned a CAPTCHA or anti-bot challenge")
    if "<title>sign in" in lowered or "/user/sign_in" in lowered:
        raise AuthError("Goodreads requires access to the bookshelf RSS feed")
    try:
        root = ET.fromstring(payload)
    except (DefusedXmlException, ET.ParseError) as exc:
        raise StructureChangedError("Goodreads bookshelf RSS is not valid XML") from exc
    nodes = root.findall("./channel/item")
    if not nodes:
        raise StructureChangedError("Goodreads bookshelf RSS has no items")
    return [_rss_row(item) for item in nodes]


def _rss_row(item: Element) -> dict[str, str]:
    book_id = _xml_text(item, "book_id")
    title = _xml_text(item, "title")
    author = _xml_text(item, "author_name")
    guid = _xml_text(item, "guid")
    review_id = REVIEW_ID_RE.search(guid)
    if not book_id or not title or not author or review_id is None:
        raise StructureChangedError(
            "Goodreads RSS item has no book id, title, author, or review id"
        )
    shelves = _xml_text(item, "user_shelves")
    return {
        "Book Id": book_id,
        "Title": title,
        "Author": author,
        "ISBN": _xml_text(item, "isbn"),
        "My Rating": _xml_text(item, "user_rating"),
        "Date Read": _rss_day(_xml_text(item, "user_read_at")),
        "Date Added": _rss_day(_xml_text(item, "user_date_added")),
        "Bookshelves": shelves,
        "Exclusive Shelf": shelves.split(",")[0].strip() if shelves else "",
        "My Review": _xml_text(item, "user_review"),
        "Year Published": _xml_text(item, "book_published"),
        "Number of Pages": _xml_text(item.find("book"), "num_pages")
        if item.find("book") is not None
        else "",
        "Review Id": review_id.group(1),
        "Image URL": _xml_text(item, "book_large_image_url"),
    }


def _xml_text(element: Element | None, name: str) -> str:
    if element is None:
        return ""
    value = element.findtext(name)
    return value.strip() if isinstance(value, str) else ""


def _rss_day(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).astimezone(UTC).strftime("%Y/%m/%d")
    except (TypeError, ValueError):
        raise StructureChangedError(f"Goodreads RSS date is invalid: {value!r}") from None


def _required(row: dict[str, Any], key: str) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise ValueError(f"Goodreads row has no {key}")
    return value


def _identifier(value: object, *, isbn: bool = False) -> str | None:
    text = str(value or "").strip()
    if isbn:
        text = "".join(char for char in text if char.isdigit() or char == "X")
    return text or None


def _year(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _date(value: object) -> datetime | None:
    text = str(value or "").strip()
    for pattern in ("%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def _rating(value: object) -> Decimal | None:
    match = RATING_RE.search(str(value or ""))
    if match is None or match.group(1) == "0":
        return None
    return Decimal(match.group(1))


def _yes(value: object) -> bool | None:
    return True if str(value or "").casefold() == "yes" else None


provider = GoodreadsProvider()
