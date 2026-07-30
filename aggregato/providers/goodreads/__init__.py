"""Goodreads library-export provider.

Goodreads does not currently offer a supported personal-library API suitable for this archive, so
the implemented surface is its CSV export.  ``GoodreadsConfig.automatic_feed_url`` is deliberately
reserved for a future supported feed/export endpoint: keeping the stable provider id and a single
normalization model means that upgrade is additive rather than a manual-sync-only dead end.
"""

from __future__ import annotations

import csv
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from aggregato.domain.enums import (
    Acquisition,
    Capability,
    Confidence,
    CreatorKind,
    EntryKind,
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
from aggregato.providers.errors import ProviderError, StructureChangedError

REQUIRED_COLUMNS = {"Book Id", "Title", "Author", "ISBN", "ISBN13", "My Rating", "Date Read"}


class GoodreadsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    automatic_feed_url: HttpUrl | None = Field(
        default=None,
        description=(
            "Reserved for a future Goodreads-supported personal-library feed or export endpoint; "
            "it is intentionally not fetched until that surface is documented and implemented."
        ),
    )


class GoodreadsProvider:
    id: str = "goodreads"
    name: str = "Goodreads"
    media_types: set[MediaType] = {MediaType.BOOK}  # noqa: RUF012
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.FILE_IMPORT,
        Capability.HAS_RATINGS,
        Capability.HAS_REVIEWS,
        Capability.HAS_CREDITS,
    }
    acquisition: Acquisition = Acquisition.EXPORT
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
    schema_version: int = 1
    default_poll_interval: timedelta = timedelta(days=1)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        del cursor
        if mode is not FetchMode.IMPORT:
            if _config(ctx).automatic_feed_url is not None:
                raise ProviderError(
                    "Goodreads automatic sync is planned but not implemented "
                    "for the configured feed"
                )
            return
        path = ctx.import_path
        if path is None:
            raise ProviderError("Goodreads import mode requires an export file")
        rows = _rows(path)
        for index, row in enumerate(rows):
            native_id = row.get("Book Id", "").strip()
            if not native_id:
                raise StructureChangedError(f"Goodreads CSV row {index + 2} has no Book Id")
            yield RawRecord(native_id=native_id, payload=row)
        yield Checkpoint(cursor=Cursor(state={"import_complete": True}))

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        row = raw.payload
        title = _required(row, "Title")
        author = _required(row, "Author")
        year = _year(row.get("Year Published")) or _year(row.get("Original Publication Year"))
        read_at = _date(row.get("Date Read")) or _date(row.get("Date Added"))
        ids = [
            NormalizedExternalId(
                namespace="goodreads", value=raw.native_id, confidence=Confidence.ASSERTED
            )
        ]
        for column, namespace in (("ISBN", "isbn"), ("ISBN13", "isbn13")):
            if value := _isbn(row.get(column)):
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
                metadata={
                    "binding": row.get("Binding") or None,
                    "publisher": row.get("Publisher") or None,
                    "pages": row.get("Number of Pages") or None,
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
        if config.automatic_feed_url is not None:
            return CheckResult(
                ok=False,
                detail=(
                    "automatic Goodreads synchronization is reserved for a future "
                    "supported endpoint"
                ),
            )
        return CheckResult(ok=True, detail="Goodreads is ready to import a library-export CSV")


def _config(ctx: ProviderContext) -> GoodreadsConfig:
    if not isinstance(ctx.config, GoodreadsConfig):
        raise ProviderError("Goodreads received an invalid configuration model")
    return ctx.config


def _rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None or not set(reader.fieldnames) >= REQUIRED_COLUMNS:
                raise StructureChangedError(
                    "not a Goodreads library-export CSV: required columns are missing"
                )
            return [dict(row) for row in reader]
    except OSError as exc:
        raise ProviderError(f"cannot read Goodreads export: {exc}") from exc


def _required(row: dict[str, Any], key: str) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise ValueError(f"Goodreads row has no {key}")
    return value


def _isbn(value: object) -> str | None:
    text = "".join(char for char in str(value or "") if char.isdigit() or char == "X")
    return text or None


def _year(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _date(value: object) -> datetime | None:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y/%m/%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _rating(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
        return parsed if parsed > 0 else None
    except Exception:
        return None


def _yes(value: object) -> bool | None:
    return True if str(value or "").casefold() == "yes" else None


provider = GoodreadsProvider()
