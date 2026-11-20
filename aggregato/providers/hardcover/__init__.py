"""Hardcover provider using the official GraphQL API.

https://docs.hardcover.app/api/getting-started/ — the same Hasura endpoint the website and apps use,
authenticated with the account owner's personal access token. One raw record is one ``user_books``
row: the account's library entry for a book, with its read-throughs nested inside it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from aggregato.domain.enums import (
    Acquisition,
    Capability,
    Confidence,
    CreatorKind,
    EntryKind,
    LoggedPrecision,
    MediaType,
    ReviewFormat,
    Role,
    ScaleKind,
)
from aggregato.domain.identifiers import IdentifierError, canonicalize_identifier
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
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import AuthError, ProviderError, StructureChangedError

API_URL = "https://api.hardcover.app/v1/graphql"

#: Library entries per request. Each request costs one unit of the account's rate limit whatever
#: its size (only top-level fields count), so a large page is the polite choice; the walk stops on
#: an empty page rather than a short one, so a server-side cap below this is still read in full.
PAGE_SIZE = 100

SCALE = RatingScale(
    id="hardcover-5-star-halves",
    kind=ScaleKind.LINEAR,
    min_value=Decimal("0.5"),
    max_value=Decimal(5),
    step=Decimal("0.5"),
)

# Hardcover's ``status_id`` vocabulary (docs: User Books → Reading Statuses).
WANT_TO_READ, READING, READ, PAUSED, DID_NOT_FINISH, IGNORED = range(1, 7)

#: ``user_book_reads.finished_at_precision``: how much of a finished date the reader remembered.
_FINISHED_PRECISION = {1: LoggedPrecision.DAY, 2: LoggedPrecision.MONTH, 3: LoggedPrecision.YEAR}

_ROLES = {
    "author": Role.AUTHOR,
    "illustrator": Role.ILLUSTRATOR,
    "translator": Role.TRANSLATOR,
    "editor": Role.EDITOR,
    "narrator": Role.NARRATOR,
}


class HardcoverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(
        min_length=1,
        description=(
            "Hardcover API token, from Settings -> Hardcover API on hardcover.app. It needs the "
            "read:me and read:library scopes. Use a ${ENV_VAR} reference to keep it out of the "
            "config file."
        ),
        json_schema_extra={"writeOnly": True},
    )


class HardcoverProvider:
    id: str = "hardcover"
    name: str = "Hardcover"
    media_types: set[MediaType] = {MediaType.BOOK}  # noqa: RUF012
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.BACKFILL,
        Capability.HAS_RATINGS,
        Capability.HAS_REVIEWS,
        Capability.HAS_CREDITS,
    }
    acquisition: Acquisition = Acquisition.API
    config_model: type[BaseModel] = HardcoverConfig
    rating_scales: list[RatingScale] = [SCALE]  # noqa: RUF012
    schema_version: int = 1
    # A library is mutable state re-read in full each run; six hourly walks of a few thousand
    # books stay far inside the free plan's 5,000 requests a day.
    default_poll_interval: timedelta = timedelta(hours=6)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: object
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        """Walk the account's whole library in ``id`` order.

        A library entry is state, not an event — a status, rating or read date can change on any
        row at any time — so every run reads all of it, as AniList does. The keyset cursor exists
        only so a run that fails partway resumes where it stopped; a completed walk checkpoints an
        empty cursor, so the next run starts from the beginning again.

        Raises:
            AuthError: The token is missing, expired, revoked, or lacks a needed scope.
            StructureChangedError: The API answered with GraphQL errors or an unexpected shape.
        """
        token = _token(ctx)
        user_id = await _user_id(ctx, token)
        after = int((cursor.state if cursor else {}).get("after_id", 0))
        while True:
            data = await _query(
                ctx, token, _LIBRARY, {"user_id": user_id, "after": after, "limit": PAGE_SIZE}
            )
            page = data.get("user_books")
            if not isinstance(page, list):
                raise StructureChangedError("Hardcover user_books is not a list")
            if not page:
                break
            for item in page:
                yield RawRecord(native_id=str(item["id"]), payload=item)
            after = int(page[-1]["id"])
            yield Checkpoint(cursor=Cursor(state={"after_id": after}))
        yield Checkpoint(cursor=Cursor(state={}))

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        item = raw.payload
        book = item["book"]
        edition = item.get("edition") or {}
        title = book.get("title")
        if not title:
            raise ValueError("Hardcover book has no title")

        credits, creator_ids = _credits(book.get("contributions") or [])
        entries = _entries(item)
        rating = item.get("rating")
        review = (item.get("review_raw") or "").strip() or None
        has_rating = rating not in (None, 0)
        return NormalizedBatch(
            work=NormalizedWork(
                media_type=MediaType.BOOK,
                title=title,
                release_year=book.get("release_year"),
                image_url=(book.get("image") or {}).get("url"),
                metadata={
                    "hardcover_slug": book.get("slug"),
                    "subtitle": book.get("subtitle"),
                    "pages": book.get("pages"),
                    "edition_format": edition.get("edition_format"),
                    "status": item.get("status"),
                    "owned": item.get("owned"),
                    "date_added": item.get("date_added"),
                },
            ),
            entries=entries,
            # The whole library is re-read each run, so an entry with nothing left to attest —
            # moved back to Want to Read, or its reads deleted — withdraws what it logged before.
            retracts_entries=not entries,
            opinions=[]
            if not has_rating and review is None
            else [
                NormalizedOpinion(
                    rating_raw=Decimal(str(rating)) if has_rating else None,
                    rating_scale_id=SCALE.id if has_rating else None,
                    review_text=review,
                    review_format=ReviewFormat.PLAIN if review else None,
                    contains_spoilers=item.get("review_has_spoilers") if review else None,
                    authored_at=_timestamp(item.get("reviewed_at")),
                )
            ],
            credits=credits,
            external_ids=_external_ids(book, edition),
            creator_external_ids=creator_ids,
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        """Read the account and one library page: proves the token and both scopes it needs."""
        try:
            async for _ in self.fetch(ctx, None, None):
                break
        except ProviderError as exc:
            return CheckResult(ok=False, error_class=exc.error_class, detail=str(exc))
        return CheckResult(ok=True, detail="Hardcover accepted the token and returned the library")


def _token(ctx: ProviderContext) -> str:
    if not isinstance(ctx.config, HardcoverConfig):
        raise ProviderError("Hardcover received an invalid configuration model")
    # Hardcover's settings page has shown tokens with their "Bearer " prefix; accept either form.
    return ctx.config.token.get_secret_value().strip().removeprefix("Bearer ").strip()


async def _user_id(ctx: ProviderContext, token: str) -> int:
    """The token owner's id. ``me`` is a Hasura function, so it answers with a one-row list."""
    me = (await _query(ctx, token, _ME, {})).get("me")
    row = me[0] if isinstance(me, list) and me else me
    if not isinstance(row, dict) or not isinstance(row.get("id"), int):
        raise AuthError("Hardcover returned no account for this token")
    return row["id"]


async def _query(
    ctx: ProviderContext, token: str, query: str, variables: dict[str, Any]
) -> dict[str, Any]:
    response = await ctx.http.post(
        API_URL,
        json={"query": query, "variables": variables},
        headers={"authorization": f"Bearer {token}"},
    )
    if response.status_code in (401, 403):
        # 401 is a missing, invalid or expired token; 403 is mostly a missing scope, and names it.
        body = _json(response)
        detail = body.get("error_description") or body.get("error") or body.get("message")
        scope = f" (needs scope {body['scope']})" if body.get("scope") else ""
        raise AuthError(f"Hardcover refused the token: {detail or response.status_code}{scope}")
    if response.status_code != 200:
        raise ProviderError(f"Hardcover answered HTTP {response.status_code}")
    body = _json(response)
    if errors := body.get("errors"):
        messages = "; ".join(str(e.get("message", e)) for e in errors if isinstance(e, dict))
        codes = {(e.get("extensions") or {}).get("code") for e in errors if isinstance(e, dict)}
        if codes & {"invalid-jwt", "invalid-headers", "access-denied"}:
            raise AuthError(f"Hardcover refused the token: {messages}")
        # A field this provider asks for no longer exists, or a filter became disallowed.
        raise StructureChangedError(f"Hardcover rejected the query: {messages or errors}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise StructureChangedError("Hardcover response has no data object")
    return data


def _json(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _entries(item: dict[str, Any]) -> list[NormalizedEntry]:
    """The read events a library entry attests to — possibly none.

    Each finished read-through is a read on its finish date, at the precision the reader gave it.
    The newest unfinished read is the one in progress: it is a ``drop`` when the book is marked Did
    Not Finish and a ``progress`` entry while it is Reading or Paused. Both reuse the read's own id,
    so finishing or abandoning it later rewrites the same entry. Older unfinished reads are reads
    the platform itself left behind and state nothing.

    Reads carry no timestamp of their own for progress or a drop, so those are dated by the library
    entry's ``updated_at``. A finished read is never dated that way: an undated read logs nothing.
    """
    reads = item.get("user_book_reads") or []
    status = item.get("status")
    updated_at = _timestamp(item.get("updated_at"))
    found: list[NormalizedEntry] = []
    for read in reads:
        if finished := _timestamp(read.get("finished_at")):
            found.append(
                NormalizedEntry(
                    kind=EntryKind.READ,
                    logged_at=finished,
                    logged_precision=_FINISHED_PRECISION.get(
                        read.get("finished_at_precision"), LoggedPrecision.DAY
                    ),
                    native_id=str(read["id"]),
                )
            )

    unfinished = [read for read in reads if not read.get("finished_at")]
    current = unfinished[-1] if unfinished else None
    if current is not None and updated_at is not None:
        if status == DID_NOT_FINISH:
            found.append(
                NormalizedEntry(
                    kind=EntryKind.DROP,
                    logged_at=updated_at,
                    logged_precision=LoggedPrecision.EXACT,
                    native_id=str(current["id"]),
                )
            )
        elif status in (READING, PAUSED) and (progress := _progress(current)):
            found.append(
                NormalizedEntry(
                    kind=EntryKind.PROGRESS,
                    logged_at=updated_at,
                    logged_precision=LoggedPrecision.EXACT,
                    native_id=str(current["id"]),
                    progress_value=progress[0],
                    progress_unit=progress[1],
                )
            )

    # Imported entries can be marked Read with a date and no read-through rows. The platform gives
    # that read no id, so it is deduplicated on its date instead.
    if not reads and status == READ and (last := _timestamp(item.get("last_read_date"))):
        found.append(
            NormalizedEntry(
                kind=EntryKind.READ, logged_at=last, logged_precision=LoggedPrecision.DAY
            )
        )
    return found


def _progress(read: dict[str, Any]) -> tuple[Decimal, str] | None:
    for key, unit in (("progress_pages", "pages"), ("progress_seconds", "seconds")):
        value = read.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return Decimal(value), unit
    return None


def _credits(
    contributions: list[dict[str, Any]],
) -> tuple[list[NormalizedCredit], list[NormalizedCreatorId]]:
    credits: list[NormalizedCredit] = []
    creator_ids: list[NormalizedCreatorId] = []
    for contribution in contributions:
        author = contribution.get("author") or {}
        name = author.get("name")
        if not name:
            continue
        # Hardcover leaves ``contribution`` null for the primary author and names every other role.
        role_raw = contribution.get("contribution") or "Author"
        credits.append(
            NormalizedCredit(
                creator_name=name,
                creator_kind=CreatorKind.PERSON,
                role=_ROLES.get(role_raw.casefold(), Role.OTHER),
                role_raw=role_raw,
                position=len(credits),
            )
        )
        if author.get("id") is not None:
            creator_ids.append(
                NormalizedCreatorId(
                    creator_name=name,
                    namespace="hardcover_author",
                    value=str(author["id"]),
                    confidence=Confidence.ASSERTED,
                )
            )
    return credits, creator_ids


def _external_ids(book: dict[str, Any], edition: dict[str, Any]) -> list[NormalizedExternalId]:
    candidates = [
        ("hardcover", book.get("id")),
        ("hardcover_edition", edition.get("id")),
        ("isbn13", edition.get("isbn_13")),
        ("isbn10", edition.get("isbn_10")),
        ("asin", edition.get("asin")),
    ]
    ids = []
    for namespace, value in candidates:
        if value is None or value == "":
            continue
        try:
            canonicalize_identifier(namespace, str(value))
        except IdentifierError:
            # Hardcover's catalog is user-edited and keeps ISBNs that fail their checksum (it flags
            # them with ``isbn_*_valid``). The host would reject the whole record over one; the
            # value stays in the retained payload, and the book still matches on its other ids.
            continue
        ids.append(
            NormalizedExternalId(
                namespace=namespace, value=str(value), confidence=Confidence.ASSERTED
            )
        )
    return ids


def _timestamp(value: object) -> datetime | None:
    """Parse a Hardcover ``date``, ``timestamp`` or ``timestamptz``; naive values are UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise StructureChangedError(f"Hardcover date is invalid: {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_ME = "query { me { id } }"

# ``status: status_id`` is an alias, not a rename of meaning: the value is a vocabulary code, and
# without the alias the payload would present it as an identifier to extract.
_LIBRARY = """
query ($user_id: Int!, $after: Int!, $limit: Int!) {
  user_books(
    where: {user_id: {_eq: $user_id}, id: {_gt: $after}}
    order_by: {id: asc}
    limit: $limit
  ) {
    id
    status: status_id
    rating
    review_raw
    review_has_spoilers
    reviewed_at
    date_added
    updated_at
    last_read_date
    owned
    book {
      id
      title
      subtitle
      slug
      release_year
      pages
      image { url }
      contributions { contribution author { id name } }
    }
    edition { id isbn_10 isbn_13 asin edition_format }
    user_book_reads(order_by: {id: asc}) {
      id
      finished_at
      finished_at_precision
      progress_pages
      progress_seconds
    }
  }
}
"""

provider = HardcoverProvider()
