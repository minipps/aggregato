"""The ``fixture`` provider: recorded records from a local file, no network, ever.

It exists so the whole sync path — schedule, spawn, fetch, checkpoint, normalize, ingest — can be
exercised end to end from day one, and so the conformance suite (contract §5) has something to run
against before a real platform is wired up. It is registered exactly like any other provider, via
the same convention, with no privileged shortcut (Constitution V: no privileged plugins).

``acquisition`` is ``export``: the honest answer for a file the operator points us at. It is not an
API and it is not a scrape, and calling it ``api`` would make the acquisition hierarchy (FR-042)
mean nothing.

Wire format, matching tests/fixtures/README.md: one **page** per line of a ``.jsonl`` file, each
page an object ``{"page": n, "records": [...]}``. ``fetch`` yields a ``Checkpoint`` per page, so
cursor resumption (FR-020) is genuinely exercised rather than assumed. Reading is done in a plain
synchronous helper: the file is small, local, and the provider runs in its own child process
(research.md R2), so there is nothing for a thread hop to protect.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

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
    NormalizedCreatorId,
    NormalizedCredit,
    NormalizedEntry,
    NormalizedExternalId,
    NormalizedOpinion,
    NormalizedWork,
    RawRecord,
)
from aggregato.domain.ratings import RatingScale
from aggregato.domain.subject_ref import SubjectRef
from aggregato.providers.base import Provider, ProviderContext
from aggregato.providers.errors import ProviderError, StructureChangedError

STARS_5 = RatingScale(
    id="fixture-stars-5",
    kind=ScaleKind.LINEAR,
    min_value=Decimal("0.5"),
    max_value=Decimal(5),
    step=Decimal("0.5"),
)


class FixtureConfig(BaseModel):
    """Settings for the fixture provider — and the settings form the UI renders (FR-039).

    Flat scalars with descriptions, because the description is the label an operator reads.
    """

    model_config = ConfigDict(extra="forbid")

    path: Path = Field(
        description="Path to the recorded .jsonl file, one page of records per line.",
    )


class _Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    format: ReviewFormat
    spoilers: bool = False


class _Credit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: CreatorKind = CreatorKind.UNKNOWN
    role: Role
    role_raw: str
    credited_as: str | None = None
    ids: dict[str, str] = Field(default_factory=dict)


class _Record(BaseModel):
    """The payload shape this provider was written against.

    ``extra="forbid"``: an unexpected key means the recording no longer matches the reader, which is
    a structure change to surface rather than a field to drop silently (FR-024).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    media_type: MediaType
    title: str
    original_title: str | None = None
    year: int | None = None
    sequence_number: int | None = None
    image: str | None = None
    logged_at: AwareDatetime
    precision: LoggedPrecision
    kind: EntryKind
    subject_ref: SubjectRef | None = None
    rating: Decimal | None = None
    liked: bool | None = None
    review: _Review | None = None
    credits: list[_Credit] = Field(default_factory=list)
    ids: dict[str, str] = Field(default_factory=dict)


class FixtureProvider:
    """A provider that reads recorded records off disk. No network in any code path."""

    id: str = "fixture"
    name: str = "Fixture"
    # RUF012 wants these immutable, but contract/provider-plugin.md §1 publishes them as `set` and
    # `list`, and a provider's declared shape should match the contract a third party reads rather
    # than a lint preference. One instance per provider class, so the shared-default risk is moot.
    media_types: set[MediaType] = {  # noqa: RUF012
        MediaType.FILM,
        MediaType.TV_SERIES,
        MediaType.BOOK,
    }
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.BACKFILL,
        Capability.FILE_IMPORT,
        Capability.HAS_RATINGS,
        Capability.HAS_REVIEWS,
        Capability.HAS_CREDITS,
    }
    acquisition: Acquisition = Acquisition.EXPORT
    # Annotated as the protocol declares them, not inferred: a Protocol's mutable attributes are
    # invariant, so `type[FixtureConfig]` would not satisfy `type[BaseModel]`.
    config_model: type[BaseModel] = FixtureConfig
    rating_scales: list[RatingScale] = [STARS_5]  # noqa: RUF012
    schema_version: int = 1
    # A local file has no rate limit to respect; an hour is the boring interval, and re-reading a
    # file the operator may have replaced is the only reason to poll at all (FR-018).
    default_poll_interval: timedelta = timedelta(hours=1)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        """Yield the recorded records, page by page, checkpointing after each page.

        Args:
            ctx: Host context. ``ctx.config`` must be a ``FixtureConfig``; in ``import`` mode
                ``ctx.import_path`` overrides the configured path, which is what ``file_import``
                means here.
            cursor: ``{"next_page": n}`` from a previous checkpoint. Pages before ``n`` are
                skipped, so a resumed run produces no duplicate and no gap. ``None`` starts over.
            mode: ``full`` and ``incremental`` behave identically (the file is the whole truth, and
                pretending otherwise would fake an incremental surface the format does not have).

        Yields:
            ``RawRecord`` per record, and a ``Checkpoint`` after each page.

        Raises:
            ProviderError: ``ctx.config`` is not a ``FixtureConfig`` — a host bug, not a data one.
            StructureChangedError: The file is missing, is not JSON lines, or a line is not a page
                object with a list of records carrying string ids. Never a silent empty result
                (FR-024, FR-026).
        """
        importing = mode is FetchMode.IMPORT and ctx.import_path is not None
        path = ctx.import_path if importing else _config(ctx).path
        assert path is not None  # narrowed by `importing`; a configured path is never None
        start = _next_page(cursor)
        for page_number, records in _read_pages(path):
            if page_number < start:
                continue
            for record in records:
                yield RawRecord(native_id=record["id"], payload=record)
            # After the page, not before: the cursor names the page to read next, so a crash here
            # resumes without re-emitting what was already ingested (FR-020).
            yield Checkpoint(cursor=Cursor(state={"next_page": page_number + 1}))

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        """Map one recorded record onto the host vocabulary. Pure: no clock, no I/O, no randomness.

        Every timestamp comes from the payload, every identifier in the payload is extracted
        (FR-009), and ``role_raw`` keeps the platform's own word verbatim (FR-016).

        Args:
            raw: A record as ``fetch`` yielded it, or as replay read it back (FR-002).

        Returns:
            A ``NormalizedBatch`` — identical on every call for the same input.

        Raises:
            ValueError: The payload does not validate against the recorded shape. Pydantic's
                ``ValidationError`` is a ``ValueError``; the host records the record in
                ``ingest_failures`` and the run continues (contract §4).
        """
        record = _Record.model_validate(raw.payload)

        opinions: list[NormalizedOpinion] = []
        if record.rating is not None or record.liked is not None or record.review is not None:
            opinions.append(
                NormalizedOpinion(
                    rating_raw=record.rating,
                    rating_scale_id=STARS_5.id if record.rating is not None else None,
                    subject_ref=record.subject_ref,
                    is_liked=record.liked,
                    review_text=record.review.text if record.review else None,
                    review_format=record.review.format if record.review else None,
                    contains_spoilers=record.review.spoilers if record.review else None,
                    # The log time is the only time the recording states; inventing an
                    # `authored_at` would be the clock this method is not allowed to read.
                    authored_at=record.logged_at,
                )
            )

        credits = [
            NormalizedCredit(
                creator_name=credit.name,
                creator_kind=credit.kind,
                role=credit.role,
                role_raw=credit.role_raw,
                credited_as=credit.credited_as,
                # Payload order, because this format does not express billing (FR-016).
                position=position,
            )
            for position, credit in enumerate(record.credits)
        ]
        creator_ids = [
            NormalizedCreatorId(
                creator_name=credit.name,
                namespace=namespace,
                value=value,
                confidence=Confidence.ASSERTED,
            )
            for credit in record.credits
            for namespace, value in sorted(credit.ids.items())
        ]

        return NormalizedBatch(
            work=NormalizedWork(
                media_type=record.media_type,
                title=record.title,
                original_title=record.original_title,
                release_year=record.year,
                sequence_number=record.sequence_number,
                image_url=record.image,
            ),
            entries=[
                NormalizedEntry(
                    kind=record.kind,
                    logged_at=record.logged_at,
                    logged_precision=record.precision,
                    native_id=record.id,
                    subject_ref=record.subject_ref,
                )
            ],
            opinions=opinions,
            credits=credits,
            # sorted() so the output is order-stable across calls: purity has to survive dict
            # iteration order, which JSON parsing does not promise across payload edits.
            external_ids=[
                NormalizedExternalId(
                    namespace=namespace, value=value, confidence=Confidence.ASSERTED
                )
                for namespace, value in sorted(record.ids.items())
            ],
            creator_external_ids=creator_ids,
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        """Report whether the configured file exists and parses. There are no credentials to test.

        Args:
            ctx: Host context; ``ctx.config`` must be a ``FixtureConfig``.

        Returns:
            ``ok=True`` when every line of the file is a readable page, otherwise ``ok=False`` with
            the ``ErrorClass`` the host would have classified the equivalent failure as.
        """
        try:
            pages = _read_pages(_config(ctx).path)
        except StructureChangedError as exc:
            return CheckResult(ok=False, error_class=ErrorClass.PARSE, detail=str(exc))
        except ProviderError as exc:
            return CheckResult(ok=False, error_class=exc.error_class, detail=str(exc))
        return CheckResult(ok=True, detail=f"{len(pages)} page(s) readable")


def _config(ctx: ProviderContext) -> FixtureConfig:
    """Narrow ``ctx.config`` at the boundary rather than assuming the host got it right."""
    if not isinstance(ctx.config, FixtureConfig):
        raise ProviderError(f"fixture provider received a {type(ctx.config).__name__} config")
    return ctx.config


def _next_page(cursor: Cursor | None) -> int:
    """The page number to resume at. A cursor we cannot read starts over rather than skipping."""
    if cursor is None:
        return 1
    value = cursor.state.get("next_page")
    return value if isinstance(value, int) and value >= 1 else 1


def _read_pages(path: Path) -> list[tuple[int, list[dict[str, Any]]]]:
    """Read the whole file into ``(page_number, records)`` pairs.

    Synchronous on purpose: see the module docstring. Eager because a fixture file is small and a
    half-read file that reports success is worse than one that fails immediately.

    Raises:
        StructureChangedError: Missing file, a line that is not JSON, or a line that is not a page
            object holding records with string ``id`` fields.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StructureChangedError(f"fixture file {path} could not be read: {exc}") from exc
    return list(_parse_pages(text, path))


def _parse_pages(text: str, path: Path) -> Iterator[tuple[int, list[dict[str, Any]]]]:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            page: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StructureChangedError(f"{path}:{line_number} is not JSON: {exc}") from exc
        if not isinstance(page, dict):
            raise StructureChangedError(f"{path}:{line_number} is not a page object")
        records = page.get("records")
        number = page.get("page")
        if not isinstance(number, int) or not isinstance(records, list):
            raise StructureChangedError(
                f"{path}:{line_number} needs an integer `page` and a list of `records`"
            )
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise StructureChangedError(
                    f"{path}:{line_number} holds a record without a string `id`"
                )
        yield number, records


provider: Provider = FixtureProvider()
"""The registration point (aggregato/providers/registry.py convention 2).

Annotated as ``Provider`` so the type checker proves this class satisfies the protocol here, at the
one place that would otherwise be a runtime surprise. Constructing it does nothing — the convention
requires that.
"""
