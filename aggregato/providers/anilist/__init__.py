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
    media_types: set[MediaType] = {MediaType.ANIME_SEASON, MediaType.MANGA}  # noqa: RUF012
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
        if cursor is not None and cursor.state.get("complete"):
            yield Checkpoint(cursor=cursor)
            return
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
        yield Checkpoint(cursor=Cursor(state={"complete": True}))

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        item = raw.payload
        media = item["media"]
        media_type = MediaType.MANGA if media.get("type") == "MANGA" else MediaType.ANIME_SEASON
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
            entries=[
                NormalizedEntry(
                    kind=EntryKind.WATCH
                    if media_type is MediaType.ANIME_SEASON
                    else EntryKind.READ,
                    logged_at=timestamp,
                    logged_precision=LoggedPrecision.EXACT,
                    native_id=raw.native_id,
                )
            ],
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
    return (
        {}
        if config.token is None
        else {"Authorization": f"Bearer {config.token.get_secret_value()}"}
    )


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
    "entries { id score notes updatedAt media { id type format seasonYear title { romaji english "
    "native } coverImage { large } staff { edges { role node { id name { full } } } } "
    "studios { nodes { id name } } } } } } }"
)

provider = AniListProvider()
