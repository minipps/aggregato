"""AniList GraphQL provider (T076), using the documented API surface only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from aggregato.providers.errors import AuthError, ProviderError

API_URL = "https://graphql.anilist.co"


class AniListConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(
        min_length=1, description="AniList username whose media lists are synchronized."
    )
    token: SecretStr | None = Field(
        default=None,
        description="Optional AniList OAuth token for private lists.",
        json_schema_extra={"writeOnly": True},
    )
    api_url: str = Field(default=API_URL, description="AniList-compatible GraphQL endpoint URL.")


class AniListProvider:
    id: str = "anilist"
    name: str = "AniList"
    media_types: set[MediaType] = {MediaType.ANIME, MediaType.MANGA}  # noqa: RUF012
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.BACKFILL,
        Capability.HAS_RATINGS,
        Capability.HAS_CREDITS,
    }
    acquisition: Acquisition = Acquisition.API
    config_model: type[BaseModel] = AniListConfig
    rating_scales: list[RatingScale] = [  # noqa: RUF012
        RatingScale(
            id="anilist-10",
            # AniList's selected score format is a discrete set of labels.  Keep that fact rather
            # than claiming that a user's 7 is arithmetic evidence about the distance to an 8.
            kind=ScaleKind.ORDINAL,
            min_value=Decimal(0),
            max_value=Decimal(10),
            step=Decimal(1),
            labels={str(value): value * 10 for value in range(11)},
        )
    ]
    schema_version: int = 1
    default_poll_interval: timedelta = timedelta(hours=1)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: object
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        """Read the account's whole anime and manga collection, every run.

        No cursor, and deliberately none: ``MediaListCollection`` is a snapshot of *mutable state*,
        with no "changed since" filter to page on. A cursor that recorded "already read this" — as
        this provider used to, by checkpointing ``{"complete": True}`` and short-circuiting on it —
        made every run after the first yield nothing at all, so a title moving from ``PLANNING`` to
        ``COMPLETED``, or a brand new entry, could never be seen again. Re-reading both collections
        is two requests against a 90-per-minute limit, which is what makes that affordable.

        Yielding no ``Checkpoint`` is the honest declaration for one unpaginated request per media
        type: there is no mid-fetch position to resume from, so the provider accepts full resyncs
        (contract §1) and the host stores no cursor to go stale.
        """
        config = _config(ctx)
        # AniList now requires the collection media type.  Query both kinds so an account's anime
        # and manga history remain one provider stream and one cursor.
        for media_type in ("ANIME", "MANGA"):
            response = await ctx.http.post(
                config.api_url,
                json={
                    "query": _QUERY,
                    "variables": {"name": config.username, "type": media_type},
                },
                headers=_headers(config),
            )
            if response.status_code == 401:
                raise AuthError("AniList rejected the configured OAuth token")
            if response.status_code != 200:
                raise ProviderError(f"AniList answered HTTP {response.status_code}")
            body = response.json()
            entries = body.get("data", {}).get("MediaListCollection", {}).get("lists", [])
            for group in entries:
                for item in group.get("entries", []):
                    # The API does this filtering, but retain it at the boundary so a compatible
                    # endpoint cannot duplicate one collection in both requests.
                    if item.get("media", {}).get("type") == media_type:
                        yield RawRecord(native_id=str(item["id"]), payload=item)

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        item = raw.payload
        media = item["media"]
        media_type = MediaType.MANGA if media.get("type") == "MANGA" else MediaType.ANIME
        title = media.get("title", {})
        score = item.get("score")
        staff = media.get("staff", {}).get("edges", [])
        studios = media.get("studios", {}).get("nodes", [])
        credits = []
        creator_ids = []
        for position, edge in enumerate(staff):
            node = edge.get("node", {})
            name = node.get("name", {}).get("full")
            if not name:
                continue
            role_raw = edge.get("role", "staff")
            role = _role(role_raw)
            credits.append(
                NormalizedCredit(
                    creator_name=name,
                    creator_kind=CreatorKind.STUDIO if role is Role.STUDIO else CreatorKind.PERSON,
                    role=role,
                    role_raw=role_raw,
                    position=position,
                )
            )
            if node.get("id") is not None:
                creator_ids.append(
                    NormalizedCreatorId(
                        creator_name=name,
                        namespace="anilist",
                        value=str(node["id"]),
                        confidence=Confidence.ASSERTED,
                    )
                )
        for position, studio in enumerate(studios, start=len(credits)):
            name = studio.get("name")
            if not name:
                continue
            credits.append(
                NormalizedCredit(
                    creator_name=name,
                    creator_kind=CreatorKind.STUDIO,
                    role=Role.STUDIO,
                    role_raw="Studio",
                    position=position,
                )
            )
            if studio.get("id") is not None:
                creator_ids.append(
                    NormalizedCreatorId(
                        creator_name=name,
                        namespace="anilist",
                        value=str(studio["id"]),
                        confidence=Confidence.ASSERTED,
                    )
                )
        timestamp = datetime.fromtimestamp(item.get("updatedAt", 0), tz=UTC)
        entries = _entries(item, media_type, timestamp, raw.native_id)
        opinions = (
            []
            if score in (None, 0)
            else [
                NormalizedOpinion(
                    rating_raw=Decimal(str(score)),
                    rating_scale_id="anilist-10",
                    review_text=item.get("notes"),
                    review_format=ReviewFormat.PLAIN if item.get("notes") else None,
                    authored_at=timestamp,
                )
            ]
        )
        return NormalizedBatch(
            work=NormalizedWork(
                media_type=media_type,
                title=title.get("romaji") or title.get("english") or "Untitled",
                original_title=title.get("native"),
                release_year=(media.get("seasonYear")),
                image_url=media.get("coverImage", {}).get("large"),
                metadata={"anilist_format": media.get("format")},
            ),
            entries=entries,
            # A stated status that logs nothing is the account taking back what it logged before. A
            # payload with no ``status`` key at all is not: it was stored before this provider asked
            # for the field, and replay re-reading it knows nothing about the account's intent.
            # Retracting on that silence would empty the log of everything the older query fetched.
            retracts_entries="status" in item and not entries,
            opinions=opinions,
            credits=credits,
            external_ids=[
                NormalizedExternalId(
                    namespace="anilist", value=str(media["id"]), confidence=Confidence.ASSERTED
                )
            ],
            creator_external_ids=creator_ids,
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        try:
            async for _ in self.fetch(ctx, None, None):
                break
        except ProviderError as exc:
            return CheckResult(ok=False, error_class=exc.error_class, detail=str(exc))
        return CheckResult(
            ok=True, detail="AniList GraphQL endpoint accepted the configured account"
        )


def _config(ctx: ProviderContext) -> AniListConfig:
    if not isinstance(ctx.config, AniListConfig):
        raise ProviderError("AniList received an invalid configuration model")
    return ctx.config


def _headers(config: AniListConfig) -> dict[str, str]:
    token = config.token.get_secret_value() if config.token is not None else ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _entries(
    item: dict[str, object], media_type: MediaType, timestamp: datetime, native_id: str | None
) -> list[NormalizedEntry]:
    """The log events a list entry's ``status`` actually attests to — possibly none.

    A list entry is a *status*, not an event: ``PLANNING`` means the account intends to watch
    something and has watched none of it. Emitting a watch for it (which this provider did before)
    renders an untouched backlog as a viewing history. There is no watchlist concept in the domain
    to put it in, so a plan states nothing and produces no entry; the work, its credits and its
    identifiers are still archived from the same batch, and the raw payload retains the status
    verbatim for replay.

    ``CURRENT`` and ``PAUSED`` are partial: they become a progress entry, which the domain requires
    to carry a value — so with no episodes counted yet, they too state nothing loggable.

    Every status shares the list entry's ``native_id``, so moving a title from ``CURRENT`` to
    ``COMPLETED`` rewrites the recorded entry instead of adding a second one, and moving it back to
    ``PLANNING`` retracts it (:func:`~aggregato.ingest.writer._retract_entries`). One list entry is
    one state, and the log shows the newest one — for anime and manga alike.
    """
    screen = media_type is MediaType.ANIME
    # REPEATING covers a reread too: REWATCH is the only repeat kind the domain has, and calling a
    # reread a plain READ would lose the fact that it happened again.
    kind = {
        "COMPLETED": EntryKind.WATCH if screen else EntryKind.READ,
        "REPEATING": EntryKind.REWATCH,
        "DROPPED": EntryKind.DROP,
        "CURRENT": EntryKind.PROGRESS,
        "PAUSED": EntryKind.PROGRESS,
    }.get(str(item.get("status")))
    if kind is None:
        # PLANNING, and any status this provider was not written against: no event is claimed
        # rather than one invented.
        return []

    progress: dict[str, object] = {}
    if kind is EntryKind.PROGRESS:
        count = item.get("progress")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            return []
        progress = {
            "progress_value": Decimal(count),
            "progress_unit": "episodes" if screen else "chapters",
        }

    return [
        NormalizedEntry(
            kind=kind,
            logged_at=timestamp,
            logged_precision=LoggedPrecision.EXACT,
            # AniList's own id for the list entry. It is the same id across every status the account
            # moves the title through, which is what lets the writer's upsert replace the recorded
            # entry rather than accumulate one row per status change.
            native_id=native_id,
            **progress,  # type: ignore[arg-type]
        )
    ]


def _role(raw: str) -> Role:
    """Map AniList's open-ended staff label while retaining it verbatim on the credit."""
    label = raw.casefold()
    for keyword, role in (
        ("studio", Role.STUDIO),
        ("director", Role.DIRECTOR),
        ("writer", Role.WRITER),
        ("composer", Role.COMPOSER),
        ("voice", Role.VOICE),
        ("performer", Role.PERFORMER),
    ):
        if keyword in label:
            return role
    return Role.OTHER


_QUERY = (
    "query ($name: String, $type: MediaType!) { "
    "MediaListCollection(userName: $name, type: $type) { lists { "
    "entries { id status progress score notes updatedAt media { id type format seasonYear "
    "title { romaji english "
    "native } coverImage { large } staff { edges { role node { id name { full } } } } "
    "studios { nodes { id name } } } } } } }"
)

provider = AniListProvider()
