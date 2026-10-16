"""Read the operator's Serializd TV diary through its authenticated web client.

Serializd documents diary, ratings, and reviews as product features, but no documented
read-history API or export was found. This provider uses the JSON routes called by the deployed
website and is therefore classified as a scraper. It accepts a bearer token copied from the
operator's own browser session; it never submits an email or password. Diary paging is offset
based, so every run starts at page one and walks all reported diary pages instead of persisting a
cursor that can shift when new rows are added.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

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
from aggregato.domain.subject_ref import SubjectRef
from aggregato.providers.base import Provider, ProviderContext
from aggregato.providers.errors import (
    AuthError,
    BlockedError,
    ProviderError,
    RateLimited,
    StructureChangedError,
    TransportError,
)

API_BASE = "https://serializddesktop.onrender.com/api"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
DIARY_FILTER = "ALL"
RATING_SCALE = RatingScale(
    id="serializd-10-point",
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
    "access denied",
    "access blocked",
    "automated access has been blocked",
    "robot check",
    "challenge-platform",
    "cf-chl-",
)


class SerializdConfig(BaseModel):
    """Credentials for reading the configured operator's own diary."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        min_length=1,
        description="Serializd username whose own diary is synchronized.",
        json_schema_extra={"x-aggregato-public": True},
    )
    token: SecretStr = Field(
        min_length=1,
        description=(
            "Bearer token from the tvproject_credentials browser cookie for your own Serializd "
            "account. Use a ${ENV_VAR} reference; the provider never logs in or accepts an "
            "email/password."
        ),
        json_schema_extra={"writeOnly": True, "x-aggregato-public": False},
    )

    @field_validator("username")
    @classmethod
    def _username_is_one_path_segment(cls, value: str) -> str:
        username = value.strip()
        if not username or any(char in username for char in "/\\?#"):
            raise ValueError("username must be one Serializd profile name")
        if any(ord(char) < 32 for char in username):
            raise ValueError("username must not contain control characters")
        return username


class SerializdProvider:
    """Poll all pages of one authenticated Serializd diary snapshot."""

    id: str = "serializd"
    name: str = "Serializd"
    media_types: set[MediaType] = {MediaType.TV}  # noqa: RUF012
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.BACKFILL,
        Capability.HAS_RATINGS,
        Capability.HAS_REVIEWS,
        Capability.SCRAPES,
    }
    acquisition: Acquisition = Acquisition.SCRAPE
    config_model: type[BaseModel] = SerializdConfig
    rating_scales: list[RatingScale] = [RATING_SCALE]  # noqa: RUF012
    schema_version: int = 1
    # Serializd publishes no request quota. Six hours keeps the complete diary walk infrequent.
    default_poll_interval: timedelta = timedelta(hours=6)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        """Read the full diary from its first page on every poll or backfill.

        The website's page-number offset can move when diary rows are added. Re-reading from page
        one is resumable only through host idempotency, so this provider deliberately emits no
        checkpoints. A row added while later pages are being read may be picked up on the next run.
        """
        del cursor
        if mode not in {FetchMode.INCREMENTAL, FetchMode.FULL}:
            raise ProviderError("Serializd does not support file import")
        config = _config(ctx)
        await _verify_account(ctx, config)

        first_page = _diary_page(
            await _get(
                ctx,
                f"/user/{quote(config.username, safe='')}/diary",
                params={"page": 1, "include_target": DIARY_FILTER},
            )
        )
        pages = first_page["total_pages"]
        if pages > 1 and not first_page["reviews"]:
            raise TransportError("Serializd diary page count changed while the first page was read")
        seen_ids: set[str] = set()
        for page_number in range(1, pages + 1):
            page = (
                first_page
                if page_number == 1
                else _diary_page(
                    await _get(
                        ctx,
                        f"/user/{quote(config.username, safe='')}/diary",
                        params={"page": page_number, "include_target": DIARY_FILTER},
                    )
                )
            )
            if page["total_pages"] != pages:
                raise TransportError("Serializd diary page count changed during pagination")
            if page_number > 1 and not page["reviews"]:
                raise TransportError("Serializd diary returned an empty page before its final page")
            for row in page["reviews"]:
                row_id = _required_id(row.get("id"), "diary id")
                if row_id in seen_ids:
                    raise TransportError("Serializd diary shifted during pagination")
                seen_ids.add(row_id)
                _required_id(row.get("showId"), "show id")
                _required_text(row.get("showName"), "show name")
                yield RawRecord(native_id=row_id, payload=row)

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        """Normalize one retained diary row without network access or inferred watch times."""
        row = raw.payload
        row_id = _required_id(row.get("id"), "diary id")
        if raw.native_id != row_id:
            raise ValueError("Serializd raw record ID does not match its diary id")

        show_id = _required_id(row.get("showId"), "show id")
        title = _required_text(row.get("showName"), "show name")
        seasons = _seasons(row.get("showSeasons"))
        subject_ref = _subject(row, seasons)

        entry_metadata: dict[str, Any] = {}
        episode_name = _optional_text(row.get("episodeName"), "episode name")
        if episode_name is not None:
            entry_metadata["episode_name"] = episode_name
        is_log = row.get("isLog")
        if not isinstance(is_log, bool):
            raise StructureChangedError("Serializd diary row has no boolean isLog state")
        entry_metadata["is_log"] = is_log
        if subject_ref is not None:
            season = next(
                season
                for season in seasons
                if _id_key(season.get("id")) == _id_key(row.get("seasonId"))
            )
            season_name = _optional_text(season.get("name"), "season name")
            if season_name is not None:
                entry_metadata["season_name"] = season_name

        is_rewatch = row.get("isRewatch", False)
        if not isinstance(is_rewatch, bool):
            raise ValueError("Serializd isRewatch must be a boolean")
        entries = []
        if is_log:
            entries.append(
                NormalizedEntry(
                    kind=EntryKind.REWATCH if is_rewatch else EntryKind.WATCH,
                    logged_at=_backdate(row.get("backdate")),
                    logged_precision=LoggedPrecision.DAY,
                    native_id=row_id,
                    subject_ref=subject_ref,
                    metadata=entry_metadata,
                )
            )
        opinion = _opinion(row, subject_ref)

        release_year = _release_year(row.get("showPremiereDate"))
        image_url = _image_url(row.get("showBannerImage"))
        external_ids = [
            NormalizedExternalId(
                namespace="serializd", value=show_id, confidence=Confidence.ASSERTED
            )
        ]
        seen_season_ids: set[str] = set()
        for season in seasons:
            season_id = _required_id(season.get("id"), "season id")
            if season_id not in seen_season_ids:
                external_ids.append(
                    NormalizedExternalId(
                        namespace="serializd_season",
                        value=season_id,
                        confidence=Confidence.ASSERTED,
                    )
                )
                seen_season_ids.add(season_id)

        return NormalizedBatch(
            work=NormalizedWork(
                media_type=MediaType.TV,
                title=title,
                release_year=release_year,
                image_url=image_url,
            ),
            entries=entries,
            opinions=[opinion] if opinion is not None else [],
            external_ids=external_ids,
            retracts_entries=not is_log,
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        """Validate the bearer account and one diary page for the configured username."""
        try:
            config = _config(ctx)
            await _verify_account(ctx, config)
            page = _diary_page(
                await _get(
                    ctx,
                    f"/user/{quote(config.username, safe='')}/diary",
                    params={"page": 1, "include_target": DIARY_FILTER},
                )
            )
            if page["reviews"]:
                row = page["reviews"][0]
                self.normalize(
                    RawRecord(native_id=_required_id(row.get("id"), "diary id"), payload=row)
                )
        except ProviderError as exc:
            return CheckResult(ok=False, error_class=exc.error_class, detail=str(exc))
        except ValueError as exc:
            return CheckResult(ok=False, error_class=ErrorClass.PARSE, detail=str(exc))
        except Exception:
            return CheckResult(
                ok=False,
                error_class=ErrorClass.TRANSPORT,
                detail=(
                    "Serializd could not complete the account or diary check; check connectivity."
                ),
            )
        return CheckResult(
            ok=True,
            detail="Serializd accepted the configured account and returned its diary page.",
        )


def _config(ctx: ProviderContext) -> SerializdConfig:
    if not isinstance(ctx.config, SerializdConfig):
        raise ProviderError("Serializd received an invalid configuration model")
    return ctx.config


def _headers(config: SerializdConfig) -> dict[str, str]:
    token = config.token.get_secret_value().strip()
    if token.casefold().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise AuthError("Serializd bearer token is empty")
    return {
        "Accept": "application/json",
        "X-Requested-With": "serializd_vercel",
        "Authorization": f"Bearer {token}",
    }


async def _verify_account(ctx: ProviderContext, config: SerializdConfig) -> None:
    body = await _get(
        ctx,
        "/user_information",
        params={"shouldGetUserContext": "true"},
    )
    username = body.get("username")
    if not isinstance(username, str) or not username:
        raise StructureChangedError("Serializd user-information response has no account username")
    if username != config.username:
        raise AuthError("configured Serializd username does not match the bearer token account")


async def _get(
    ctx: ProviderContext,
    path: str,
    *,
    params: dict[str, str | int],
) -> dict[str, Any]:
    response = await ctx.http.get(
        f"{API_BASE}{path}", params=params, headers=_headers(_config(ctx))
    )
    _raise_for_response(response)
    try:
        body = response.json()
    except ValueError as exc:
        text = response.content.decode("utf-8", errors="replace").casefold()
        if _is_challenge(text):
            raise BlockedError("Serializd returned an anti-bot challenge; sync stopped") from exc
        raise StructureChangedError("Serializd API response is not valid JSON") from exc
    if not isinstance(body, dict):
        raise StructureChangedError("Serializd API response is not a JSON object")
    return body


def _raise_for_response(response: Any) -> None:
    status = response.status_code
    if status == 429:
        raise RateLimited(
            "Serializd rate limited the diary request",
            retry_after=_retry_after(
                response.headers.get("Retry-After"), response.headers.get("Date")
            ),
        )
    if 200 <= status < 300:
        return

    body = response.content.decode("utf-8", errors="replace").casefold()
    if _is_challenge(body):
        raise BlockedError("Serializd returned an anti-bot challenge; sync stopped")
    if status in {401, 403}:
        raise AuthError("Serializd rejected the configured bearer token")
    if status >= 500:
        raise TransportError(f"Serializd API answered HTTP {status}")
    if status == 404:
        raise StructureChangedError("Serializd diary route or configured profile was not found")
    raise ProviderError(f"Serializd API answered HTTP {status}")


def _is_challenge(body: str) -> bool:
    return any(marker in body for marker in _CHALLENGE_MARKERS)


def _retry_after(value: str | None, response_date: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        # HTTP-date Retry-After values can be converted without consulting the local clock when
        # the response also supplies its server Date.
        try:
            retry_at = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)
            if response_date is None:
                return None
            server_now = datetime.strptime(response_date, "%a, %d %b %Y %H:%M:%S GMT").replace(
                tzinfo=UTC
            )
        except ValueError:
            return None
        seconds = (retry_at - server_now).total_seconds()
    return max(0.0, seconds)


def _diary_page(body: Mapping[str, Any]) -> dict[str, Any]:
    reviews = body.get("reviews")
    pages = body.get("totalPages")
    if not isinstance(reviews, list):
        raise StructureChangedError("Serializd diary response has no reviews list")
    if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
        raise StructureChangedError("Serializd diary response has an invalid totalPages value")
    if not all(isinstance(row, dict) for row in reviews):
        raise StructureChangedError("Serializd diary reviews contain a non-object row")
    if pages == 0 and reviews:
        raise StructureChangedError("Serializd returned diary rows with totalPages equal to zero")
    return {"reviews": reviews, "total_pages": pages}


def _seasons(value: object) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(season, dict) for season in value):
        raise StructureChangedError("Serializd showSeasons is not a list of season objects")
    return value


def _subject(row: Mapping[str, Any], seasons: list[Mapping[str, Any]]) -> SubjectRef | None:
    season_id = row.get("seasonId")
    episode_value = row.get("episodeNumber")
    episode_number = _optional_positive_int(episode_value, "episode number")
    if season_id is None:
        if episode_number is not None:
            raise ValueError("Serializd diary episode has no season id")
        return None

    season_key = _id_key(season_id)
    season = next(
        (item for item in seasons if _id_key(item.get("id")) == season_key),
        None,
    )
    if season is None:
        raise StructureChangedError("Serializd diary season id is absent from showSeasons")
    number = _required_int(season.get("seasonNumber"), "season number")
    if number == 0:
        raise ValueError(
            "Serializd special episodes use season zero, which subject_ref cannot represent"
        )
    if number < 0:
        raise ValueError("Serializd season number must be positive")
    return SubjectRef(season=number, episode=episode_number)


def _opinion(row: Mapping[str, Any], subject_ref: SubjectRef | None) -> NormalizedOpinion | None:
    rating = _rating(row.get("rating"))
    review_value = row.get("reviewText")
    if review_value is not None and not isinstance(review_value, str):
        raise ValueError("Serializd review text must be a string or null")
    review = review_value if isinstance(review_value, str) and review_value != "" else None
    liked_value = row.get("like")
    if liked_value is not None and not isinstance(liked_value, bool):
        raise ValueError("Serializd like must be a boolean or null")
    spoiler_value = row.get("containsSpoiler")
    if spoiler_value is not None and not isinstance(spoiler_value, bool):
        raise ValueError("Serializd containsSpoiler must be a boolean or null")
    if rating is None and review is None and liked_value is None and spoiler_value is None:
        return None
    return NormalizedOpinion(
        rating_raw=rating,
        rating_scale_id=RATING_SCALE.id if rating is not None else None,
        subject_ref=subject_ref,
        is_liked=liked_value,
        review_text=review,
        review_format=ReviewFormat.PLAIN if review is not None else None,
        contains_spoilers=spoiler_value,
    )


def _rating(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Serializd rating must be an integer from 1 to 10")
    try:
        rating = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Serializd rating must be an integer from 1 to 10") from exc
    if not rating.is_finite():
        raise ValueError("Serializd rating must be finite")
    if rating == 0:
        return None
    if not RATING_SCALE.admits(rating):
        raise ValueError("Serializd rating is outside the 1–10 point scale")
    return rating


def _backdate(value: object) -> datetime:
    if not isinstance(value, str) or len(value) < 10:
        raise StructureChangedError("Serializd diary row has no ISO backdate")
    try:
        logged_day = date.fromisoformat(value[:10])
        if len(value) > 10:
            if value[10] not in {"T", "t", " "}:
                raise ValueError
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StructureChangedError("Serializd diary backdate is not an ISO date") from exc
    return datetime.combine(logged_day, time.min, tzinfo=UTC)


def _release_year(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 9999:
        return value
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


def _image_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parts = urlsplit(candidate)
    if parts.scheme == "https" and parts.netloc:
        return candidate
    if candidate.startswith("/"):
        return f"{TMDB_IMAGE_BASE}{candidate}"
    return None


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _required_int(value, label, positive=True)


def _required_int(value: object, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Serializd {label} must be an integer")
    try:
        number = int(value) if isinstance(value, str) and value.isdigit() else value
    except ValueError as exc:
        raise ValueError(f"Serializd {label} must be an integer") from exc
    if not isinstance(number, int):
        raise ValueError(f"Serializd {label} must be an integer")
    if positive and number <= 0:
        raise ValueError(f"Serializd {label} must be positive")
    return number


def _required_id(value: object, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise StructureChangedError(f"Serializd record has no usable {label}")
    text = str(value).strip()
    if not text:
        raise StructureChangedError(f"Serializd record has no usable {label}")
    return text


def _id_key(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    return text or None


def _required_text(value: object, label: str) -> str:
    text = _optional_text(value, label)
    if text is None:
        raise StructureChangedError(f"Serializd record has no {label}")
    return text


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Serializd {label} must be a string or null")
    text = value.strip()
    return text or None


provider: Provider = SerializdProvider()
