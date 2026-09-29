"""Spotify listening history through its user-authorized Web API.

The API is the highest acquisition surface: ``GET /v1/me/player/recently-played`` returns
timestamped tracks and supports time cursors. The provider stores one track listen per API item;
Spotify's separate lifetime export is not needed for this polling path.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
    Checkpoint,
    CheckResult,
    Cursor,
    NormalizedBatch,
    NormalizedCreatorId,
    NormalizedCredit,
    NormalizedEntry,
    NormalizedExternalId,
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
)

API_URL = "https://api.spotify.com/v1/me/player/recently-played"
OAUTH_ENDPOINT = "https://accounts.spotify.com/api/token"
PAGE_SIZE = 50
_ID_KEYS = frozenset(
    {
        "asin",
        "ean",
        "external_ids",
        "guid",
        "gtin",
        "id",
        "identifiers",
        "ids",
        "isbn",
        "isbn10",
        "isbn13",
        "isrc",
        "mbid",
        "upc",
        "uri",
        "uuid",
    }
)


class SpotifyConfig(BaseModel):
    """Authorization Code credentials for the Spotify Web API."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(
        min_length=1,
        description="Client ID from the Spotify Developer Dashboard.",
    )
    client_secret: SecretStr = Field(
        min_length=1,
        description=(
            "Spotify app client secret. Use a ${ENV_VAR} reference to keep it out of the config "
            "file."
        ),
        json_schema_extra={"writeOnly": True},
    )
    refresh_token: SecretStr = Field(
        min_length=1,
        description=(
            "Refresh token authorized with the user-read-recently-played scope. Spotify refresh "
            "tokens expire after six months; reauthorize when one expires. Development Mode also "
            "requires a Premium app owner and an allowlisted account. Use a ${ENV_VAR} reference "
            "to keep it out of the config file."
        ),
        json_schema_extra={"writeOnly": True},
    )


class SpotifyProvider:
    id: str = "spotify"
    name: str = "Spotify"
    media_types: set[MediaType] = {MediaType.TRACK}  # noqa: RUF012
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.BACKFILL,
        Capability.HAS_CREDITS,
    }
    acquisition: Acquisition = Acquisition.API
    config_model: type[BaseModel] = SpotifyConfig
    rating_scales: list[RatingScale] = []  # noqa: RUF012
    schema_version: int = 1
    default_poll_interval: timedelta = timedelta(minutes=15)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        config = _config(ctx)
        token = await _access_token(ctx, config)
        state = cursor.state if cursor is not None else {}
        newest_ms = _as_int(state.get("newest_ms"))
        resume_before = _as_int(state.get("before_ms"))
        resuming = state.get("walk_mode") == mode.value and resume_before is not None
        floor_ms = _as_int(state.get("floor_ms")) if resuming else None

        if resuming:
            before_ms = resume_before
            after_ms = None
        elif mode is FetchMode.INCREMENTAL and newest_ms is not None:
            floor_ms = newest_ms
            before_ms = None
            # Include the previous millisecond to avoid dropping simultaneous plays; writes are
            # idempotent, so the overlap is safe.
            after_ms = max(0, newest_ms - 1)
        else:
            before_ms = after_ms = None

        while True:
            params: dict[str, int] = {"limit": PAGE_SIZE}
            if before_ms is not None:
                params["before"] = before_ms
            elif after_ms is not None:
                params["after"] = after_ms
            page = await _page(ctx, token, params)
            items = page["items"]
            if not items:
                yield _checkpoint(newest_ms)
                return

            played_ms = [_timestamp_ms(item["played_at"]) for item in items]
            for item, timestamp in zip(items, played_ms, strict=True):
                newest_ms = timestamp if newest_ms is None else max(timestamp, newest_ms)
                if floor_ms is None or timestamp >= floor_ms:
                    yield RawRecord(native_id=_native_id(item), payload=item)

            oldest_ms = min(played_ms)
            # ponytail: Spotify's millisecond cursor cannot page 50+ plays with the same timestamp;
            # upgrade if the API adds a stable event-id cursor.
            next_before = _as_int(page.get("before"))
            if next_before is None or (before_ms is not None and next_before >= before_ms):
                raise StructureChangedError(
                    "Spotify returned a missing or non-advancing `before` cursor"
                )
            before_ms = next_before
            yield Checkpoint(
                cursor=Cursor(
                    state={
                        "newest_ms": newest_ms,
                        "walk_mode": mode.value,
                        "floor_ms": floor_ms,
                        "before_ms": before_ms,
                    }
                )
            )
            after_ms = None
            if floor_ms is not None and oldest_ms <= floor_ms:
                yield _checkpoint(newest_ms)
                return

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        item = raw.payload
        track = _mapping(item.get("track"))
        title = _text(track.get("name"))
        if title is None:
            raise StructureChangedError("Spotify play has no track name")
        played_at = _played_at(item.get("played_at"))
        album = _mapping(track.get("album"))
        artists = _artist_list(track.get("artists"), "track.artists")
        album_artists = _artist_list(album.get("artists"), "track.album.artists")
        track_id = _text(track.get("id"))

        external_ids = [
            NormalizedExternalId(namespace=namespace, value=value, confidence=Confidence.ASSERTED)
            for namespace, value in _work_ids(
                item, track_id or _text(track.get("uri")) or raw.native_id
            )
        ]
        creator_ids = [
            NormalizedCreatorId(
                creator_name=name,
                namespace=namespace,
                value=value,
                confidence=Confidence.ASSERTED,
            )
            for artist in [*artists, *album_artists]
            if (name := _text(artist.get("name")))
            for namespace, value in _artist_ids(artist)
        ]
        release_date = _text(album.get("release_date"))
        images = album.get("images")
        image_url = (
            _text(images[0].get("url"))
            if isinstance(images, list) and images and isinstance(images[0], dict)
            else None
        )
        if image_url is not None and not image_url.startswith("https://"):
            image_url = None
        metadata = {
            key: value
            for key, value in {
                "album": _text(album.get("name")),
                "release_date": release_date,
                "release_date_precision": _text(album.get("release_date_precision")),
                "disc_number": track.get("disc_number"),
                "duration_ms": track.get("duration_ms"),
                "is_local": track.get("is_local"),
            }.items()
            if value is not None
        }
        return NormalizedBatch(
            work=NormalizedWork(
                media_type=MediaType.TRACK,
                title=title,
                release_year=_release_year(release_date),
                sequence_number=_as_int(track.get("track_number")),
                image_url=image_url,
                metadata=metadata,
            ),
            entries=[
                NormalizedEntry(
                    kind=EntryKind.LISTEN,
                    logged_at=played_at,
                    logged_precision=LoggedPrecision.EXACT,
                    native_id=raw.native_id,
                )
            ],
            credits=[
                NormalizedCredit(
                    creator_name=_text(artist.get("name")) or "",
                    creator_kind=CreatorKind.UNKNOWN,
                    role=Role.PERFORMER,
                    role_raw="artists",
                    position=position,
                )
                for position, artist in enumerate(artists)
            ],
            external_ids=_unique_external_ids(external_ids),
            creator_external_ids=_unique_creator_ids(creator_ids),
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        try:
            token = await _access_token(ctx, _config(ctx))
            await _page(ctx, token, {"limit": 1})
        except ProviderError as exc:
            return CheckResult(ok=False, error_class=exc.error_class, detail=str(exc))
        except Exception as exc:
            return CheckResult(
                ok=False,
                error_class=ErrorClass.TRANSPORT,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return CheckResult(ok=True, detail="Spotify accepted the configured account")


async def _access_token(ctx: ProviderContext, config: SpotifyConfig) -> str:
    basic = base64.b64encode(
        f"{config.client_id}:{config.client_secret.get_secret_value()}".encode()
    ).decode()
    response = await ctx.http.post(
        OAUTH_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": config.refresh_token.get_secret_value(),
        },
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    if response.status_code in {400, 401}:
        raise AuthError(
            "Spotify rejected the client credentials or refresh token; check the app settings "
            "and reauthorize with `user-read-recently-played`"
        )
    _raise_status(response.status_code, OAUTH_ENDPOINT)
    body = _json_object(response, "Spotify token response")
    token = _text(body.get("access_token"))
    if token is None:
        raise StructureChangedError("Spotify token response has no access token")
    return token


async def _page(ctx: ProviderContext, token: str, params: dict[str, int]) -> dict[str, Any]:
    response = await ctx.http.get(
        API_URL,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    _raise_status(response.status_code, API_URL)
    body = _json_object(response, "Spotify recently-played response")
    items = body.get("items")
    if not isinstance(items, list):
        raise StructureChangedError("Spotify recently-played response has no `items` list")
    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("track"), dict):
            raise StructureChangedError("Spotify recently-played item has no track object")
        if _text(item["track"].get("name")) is None:
            raise StructureChangedError("Spotify recently-played track has no name")
        _played_at(item.get("played_at"))
        _native_id(item)
        records.append(item)
    cursors = body.get("cursors")
    before = _as_int(cursors.get("before")) if isinstance(cursors, dict) else None
    if records and before is None:
        raise StructureChangedError("Spotify response has records but no `before` cursor")
    return {"items": records, "before": before}


def _raise_status(status: int, url: str) -> None:
    if status == 401:
        raise AuthError(f"{url} rejected Spotify authorization (HTTP 401)")
    if status in {403, 451}:
        raise BlockedError(f"{url} refused access with HTTP {status}")
    if status == 429:
        raise RateLimited(f"{url} is rate-limiting (HTTP 429)")
    if status != 200:
        raise ProviderError(f"{url} answered HTTP {status}")


def _json_object(response: Any, label: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise StructureChangedError(f"{label} is not valid JSON") from exc
    if not isinstance(body, dict):
        raise StructureChangedError(f"{label} is not a JSON object")
    return body


def _checkpoint(newest_ms: int | None) -> Checkpoint:
    return Checkpoint(cursor=Cursor(state={"newest_ms": newest_ms}))


def _native_id(item: Mapping[str, Any]) -> str:
    track = _mapping(item.get("track"))
    identifier = _text(track.get("id")) or _text(track.get("uri")) or _text(track.get("name"))
    if identifier is None:
        raise StructureChangedError("Spotify play has no stable track identifier")
    return f"{_text(item.get('played_at'))}:{identifier}"


def _played_at(value: object) -> datetime:
    text = _text(value)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise StructureChangedError("Spotify play has no timezone-aware `played_at` timestamp")
    return moment.astimezone(UTC)


def _timestamp_ms(value: object) -> int:
    moment = _played_at(value)
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _work_ids(item: Mapping[str, Any], track_id: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path, value in _identifier_values(item, skip_artists=True):
        if path == ("track", "id"):
            namespace = "spotify_track"
        elif path == ("track", "uri"):
            namespace = "spotify_track_uri"
        elif path == ("track", "external_ids", "isrc"):
            namespace = "isrc"
        else:
            # Album and context ids are retained without making different tracks match as one work.
            namespace = _scoped_namespace(track_id, path)
        found.append((namespace, value))
    return found


def _artist_ids(artist: Mapping[str, Any]) -> list[tuple[str, str]]:
    result = []
    for path, value in _identifier_values(artist):
        key = path[-1]
        namespace = "spotify_artist" if key == "id" else f"spotify_artist_{key}"
        result.append((namespace, value))
    return result


def _scoped_namespace(track_id: str, path: tuple[str, ...]) -> str:
    namespace = f"spotify_ref_{track_id}_{'_'.join(path)}"
    if len(namespace) > 64:
        namespace = f"{namespace[:47]}_{hashlib.sha256(namespace.encode()).hexdigest()[:16]}"
    return namespace


def _identifier_values(
    node: object,
    path: tuple[str, ...] = (),
    *,
    skip_artists: bool = False,
    under_identifier: bool = False,
) -> list[tuple[tuple[str, ...], str]]:
    found: list[tuple[tuple[str, ...], str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            key = str(key).lower()
            if skip_artists and key == "artists":
                continue
            child_path = (*path, key)
            is_identifier = under_identifier or key in _ID_KEYS
            if is_identifier and isinstance(value, str | int) and not isinstance(value, bool):
                if str(value):
                    found.append((child_path, str(value)))
            else:
                found.extend(
                    _identifier_values(
                        value,
                        child_path,
                        skip_artists=skip_artists,
                        under_identifier=is_identifier,
                    )
                )
    elif isinstance(node, list):
        for item in node:
            found.extend(
                _identifier_values(
                    item,
                    path,
                    skip_artists=skip_artists,
                    under_identifier=under_identifier,
                )
            )
    elif (
        under_identifier
        and isinstance(node, str | int)
        and not isinstance(node, bool)
        and str(node)
    ):
        found.append((path, str(node)))
    return found


def _artist_list(value: object, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise StructureChangedError(f"Spotify `{field}` is not a list")
    artists = []
    for artist in value:
        if not isinstance(artist, dict):
            raise StructureChangedError(f"Spotify `{field}` contains a non-object")
        if _text(artist.get("name")) is None and _identifier_values(artist):
            raise StructureChangedError(f"Spotify `{field}` has an identified artist with no name")
        if _text(artist.get("name")):
            artists.append(artist)
    return artists


def _unique_external_ids(
    values: list[NormalizedExternalId],
) -> list[NormalizedExternalId]:
    return list({(item.namespace, item.value): item for item in values}.values())


def _unique_creator_ids(
    values: list[NormalizedCreatorId],
) -> list[NormalizedCreatorId]:
    return list({(item.creator_name, item.namespace, item.value): item for item in values}.values())


def _release_year(value: str | None) -> int | None:
    if value is None or len(value) < 4 or not value[:4].isdigit():
        return None
    return int(value[:4])


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _config(ctx: ProviderContext) -> SpotifyConfig:
    if not isinstance(ctx.config, SpotifyConfig):
        raise ProviderError("Spotify received an invalid configuration model")
    return ctx.config


provider: Provider = SpotifyProvider()
