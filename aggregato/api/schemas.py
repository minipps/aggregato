"""Response shapes for the log endpoints, exactly as ``contracts/openapi.yaml`` declares them.

Two conventions the contract states and every model here obeys:

* ``logged_at`` never travels without ``logged_precision`` . Both are required fields of
  :class:`Entry`, so a client physically cannot render a fabricated time as exact.
* A rating always travels as raw value, scale, and normalized value together . All three are
  fields of :class:`Rating`, and ``normalized`` is comparable *within* a scale — it is not a claim
  that 80/100 on one platform means what 80/100 means on another.

The row-to-model functions live here rather than in the routes so the three endpoints cannot drift
into two shapes for the same object.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Final

from pydantic import BaseModel, Field
from sqlalchemy import Row

from aggregato.api.queries import WorkAggregate, aware
from aggregato.domain.enums import (
    Confidence,
    CreatorKind,
    EntryKind,
    LoggedPrecision,
    MediaFamily,
    MediaType,
    ReviewFormat,
    Role,
)
from aggregato.domain.families import family_of

#: Where cached artwork is served from (: never a redirect to the third-party host). The path
#: is derived from ``image_cache.url_hash``, which is the sha256 of the source URL;  serves the
#: bytes. Spelled out rather than imported from ``main`` because the API package must not depend on
#: the app module that includes it.
IMAGE_PATH: Final = "/api/v1/media/image/"


class Rating(BaseModel):
    """Raw value, scale, and normalized value — always all three ."""

    raw: float | None
    scale_id: str | None
    normalized: int | None


class Progress(BaseModel):
    """How far through the work, with the unit that makes the number readable."""

    value: float
    unit: str


class ExternalId(BaseModel):
    """An identifier some provider asserted for the work."""

    namespace: str
    value: str
    source: str
    confidence: Confidence


class Credit(BaseModel):
    """A creator's involvement, including how the identity was established ."""

    id: int
    creator_id: uuid.UUID
    creator_name: str
    role: Role
    role_raw: str | None
    credited_as: str | None
    position: int
    source: str
    link_confidence: Confidence


class Creator(BaseModel):
    """A creator with the aggregate counts used by list and detail views."""

    id: uuid.UUID
    kind: CreatorKind
    name: str
    image: str | None = None
    families: list[MediaFamily] = Field(default_factory=list)
    credit_count: int = 0
    logged_count: int = 0


class CreatorAlias(BaseModel):
    name: str
    media_family: MediaFamily
    kind: str


class CreatorDetail(Creator):
    aliases: list[CreatorAlias] = Field(default_factory=list)
    external_ids: list[ExternalId] = Field(default_factory=list)
    credits_by_role: dict[str, list[Credit]] = Field(default_factory=dict)


class Work(BaseModel):
    """A work. ``media_family`` is derived from ``media_type``, never stored (data-model.md §1)."""

    id: uuid.UUID
    media_type: MediaType
    media_family: MediaFamily
    title: str
    original_title: str | None = None
    release_year: int | None = None
    parent_work_id: uuid.UUID | None = None
    sequence_number: int | None = None
    image: str | None = None
    entry_count: int = 0
    providers: list[str] = Field(default_factory=list)


class Entry(BaseModel):
    """One logged event. ``logged_precision`` is required alongside ``logged_at`` ."""

    id: int
    work_id: uuid.UUID
    work: Work | None = None
    provider_id: str
    kind: EntryKind
    logged_at: datetime
    logged_precision: LoggedPrecision
    subject_ref: dict[str, int] | None = None
    progress: Progress | None = None
    ingested_at: datetime
    deleted_at: datetime | None = None


class Opinion(BaseModel):
    """A rating, a like, a review, or whatever combination the platform reported together."""

    id: int
    work_id: uuid.UUID
    provider_id: str
    rating: Rating
    subject_ref: dict[str, int] | None = None
    is_liked: bool | None = None
    review_text: str | None = None
    review_format: ReviewFormat | None = None
    contains_spoilers: bool | None = None
    authored_at: datetime | None = None


class WorkDetail(Work):
    """A work with everything a detail view needs, so the client makes one request rather than six.

    ``parent`` and ``siblings`` are what let a client walk the hierarchy — from an episode to its
    season to the other seasons — without a second query per hop.
    """

    external_ids: list[ExternalId] = Field(default_factory=list)
    credits: list[Credit] = Field(default_factory=list)
    entries: list[Entry] = Field(default_factory=list)
    opinions: list[Opinion] = Field(default_factory=list)
    parent: Work | None = None
    siblings: list[Work] = Field(default_factory=list)


class PageResponse[T](BaseModel):
    """The contract's ``Page``: ``items`` plus a nullable ``next_cursor``, and no offset .

    A pydantic model rather than :class:`~aggregato.api.pagination.Page`, which is the internal
    dataclass the keyset helper returns; this is the one that serializes.
    """

    items: list[T]
    next_cursor: str | None = None


def work_from_row(row: Row[Any], aggregate: WorkAggregate) -> Work:
    """Build a ``Work`` from a row of ``works`` plus its batched counts."""
    media_type = MediaType(row.media_type)
    return Work(
        id=row.id,
        media_type=media_type,
        media_family=family_of(media_type),
        title=row.title,
        original_title=row.original_title,
        release_year=row.release_year,
        parent_work_id=row.parent_work_id,
        sequence_number=row.sequence_number,
        image=_image(row.image_url),
        entry_count=aggregate.entry_count,
        providers=aggregate.providers,
    )


def entry_from_row(row: Row[Any], work: Work | None = None) -> Entry:
    """Build an ``Entry`` from a row of ``entries``, optionally embedding its work."""
    return Entry(
        id=row.id,
        work_id=row.work_id,
        work=work,
        provider_id=row.provider_id,
        kind=EntryKind(row.kind),
        # aware(): SQLite drops the offset, and the contract's timestamps carry one.
        logged_at=_required(aware(row.logged_at)),
        logged_precision=LoggedPrecision(row.logged_precision),
        subject_ref=row.subject_ref,
        progress=_progress(row.progress_value, row.progress_unit),
        ingested_at=_required(aware(row.ingested_at)),
        deleted_at=aware(row.deleted_at),
    )


def opinion_from_row(row: Row[Any]) -> Opinion:
    """Build an ``Opinion`` from a row of ``opinions``."""
    return Opinion(
        id=row.id,
        work_id=row.work_id,
        provider_id=row.provider_id,
        rating=Rating(
            raw=_number(row.rating_raw),
            scale_id=row.rating_scale_id,
            normalized=row.rating_normalized,
        ),
        subject_ref=row.subject_ref,
        is_liked=row.is_liked,
        review_text=row.review_text,
        review_format=ReviewFormat(row.review_format) if row.review_format else None,
        contains_spoilers=row.contains_spoilers,
        authored_at=aware(row.authored_at),
    )


def credit_from_row(row: Row[Any]) -> Credit:
    """Build a ``Credit`` from a row of ``work_credits`` joined to ``creators``."""
    return Credit(
        id=row.id,
        creator_id=row.creator_id,
        creator_name=row.creator_name,
        role=Role(row.role),
        role_raw=row.role_raw,
        credited_as=row.credited_as,
        position=row.position,
        source=row.source,
        link_confidence=Confidence(row.link_confidence),
    )


def external_id_from_row(row: Row[Any]) -> ExternalId:
    """Build an ``ExternalId`` from a row of ``external_ids``."""
    return ExternalId(
        namespace=row.namespace,
        value=row.value,
        source=row.source,
        confidence=Confidence(row.confidence),
    )


def _image(image_url: str | None) -> str | None:
    """The local path for a work's artwork, never the platform URL itself ."""
    if not image_url:
        return None
    return f"{IMAGE_PATH}{sha256(image_url.encode()).hexdigest()}"


def _progress(value: Decimal | None, unit: str | None) -> Progress | None:
    """Pair a progress value with its unit. The schema guarantees they arrive together."""
    if value is None or unit is None:
        return None
    return Progress(value=float(value), unit=unit)


def _number(value: Decimal | None) -> float | None:
    """Render a stored ``Decimal`` as the contract's ``number``.

    Storage and provider contracts use Decimal so a 0.5-step scale does not land "between steps".
    JSON has no decimal type, so conversion happens here, at the wire, and nowhere earlier.
    """
    return None if value is None else float(value)


def _required(value: datetime | None) -> datetime:
    """Assert a NOT NULL timestamp column really was not null, for the type checker."""
    assert value is not None
    return value
