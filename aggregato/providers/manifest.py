"""Host-owned metadata for bundled providers.

Discovery must be safe even when a provider package is broken or unreviewed.  The metadata used by
the API, scheduler, and parent-side writer therefore lives here rather than being imported from a
provider module.  A provider module is loaded only after a worker has selected that provider for a
child run.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from aggregato.domain.ratings import RatingScale
from aggregato.domain.enums import ScaleKind


def _object_schema(
    properties: dict[str, dict[str, Any]], *, required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _string(
    *,
    description: str,
    required: bool = False,
    secret: bool = False,
    default: str | None = None,
    format: str | None = None,
    min_length: int | None = None,
) -> tuple[dict[str, Any], bool]:
    value: dict[str, Any] = {"type": "string", "description": description}
    if default is not None:
        value["default"] = default
    if format is not None:
        value["format"] = format
    if min_length is not None:
        value["minLength"] = min_length
    if secret:
        value["writeOnly"] = True
    return value, required


def _nullable_string(*, description: str, format: str | None = None) -> dict[str, Any]:
    value, _ = _string(description=description, format=format)
    return {"anyOf": [value, {"type": "null"}], "default": None}


def _scale(
    scale_id: str,
    *,
    kind: str,
    minimum: str,
    maximum: str,
    step: str,
    labels: dict[str, int] | None = None,
) -> RatingScale:
    return RatingScale(
        id=scale_id,
        kind=ScaleKind(kind),
        min_value=Decimal(minimum),
        max_value=Decimal(maximum),
        step=Decimal(step),
        labels=labels,
    )


def _manifest(
    *,
    name: str,
    media_types: tuple[str, ...],
    capabilities: tuple[str, ...],
    acquisition: str,
    schema_version: int,
    interval_seconds: int,
    config_schema: dict[str, Any],
    rating_scales: tuple[RatingScale, ...] = (),
    import_inference: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "media_types": media_types,
        "capabilities": capabilities,
        "acquisition": acquisition,
        "schema_version": schema_version,
        "default_poll_interval_seconds": interval_seconds,
        "config_schema": config_schema,
        "rating_scales": rating_scales,
        "api_visible": True,
        "provider_api_version": 1,
        "import_inference": import_inference,
    }


_anilist_api_url, _ = _string(
    description="AniList-compatible GraphQL endpoint URL.",
    default="https://graphql.anilist.co",
)
_anilist_token, _ = _string(
    description="Optional AniList OAuth token for private lists.",
    secret=True,
    format="password",
)
_anilist_token = {"anyOf": [_anilist_token, {"type": "null"}], "default": None}
_anilist_username, _ = _string(
    description="AniList username whose media lists are synchronized.", required=True, min_length=1
)

_fixture_path, _ = _string(
    description="Path to the recorded .jsonl file, one page of records per line.",
    required=True,
    format="path",
)

_goodreads_profile = _nullable_string(
    description=(
        "Public Goodreads profile URL (/user/show/<id>) or RSS URL "
        "(/review/list_rss/<id>). The public RSS feed is refreshed daily."
    ),
    format="uri",
)
_goodreads_export = _nullable_string(
    description="Optional local library-export CSV for a manually requested full sync.",
    format="path",
)

_koito_base_url, _ = _string(
    description="Base URL of the Koito server: scheme, host and optional port.", required=True
)
_koito_api_key, _ = _string(
    description="Koito API key. Use a ${ENV_VAR} reference to keep it out of the config file.",
    required=True,
    secret=True,
    format="password",
    min_length=1,
)

_letterboxd_username = _nullable_string(
    description="Letterboxd username whose public RSS feed should be synchronized."
)
_letterboxd_rss = _nullable_string(
    description="Optional full public RSS URL; overrides the URL derived from username.",
    format="uri",
)
_letterboxd_export = _nullable_string(
    description="Optional local RSS/XML export for a manually requested full sync.", format="path"
)

_listenbrainz_base, _ = _string(
    description="Base URL of the ListenBrainz-compatible API.",
    default="https://api.listenbrainz.org",
)
_listenbrainz_username, _ = _string(
    description="ListenBrainz username whose listens are read.", required=True, min_length=1
)
_listenbrainz_token, _ = _string(
    description=(
        "ListenBrainz user token. Use an environment-variable reference to keep it "
        "out of the config file."
    ),
    required=True,
    secret=True,
    format="password",
    min_length=1,
)


BUNDLED_MANIFESTS: dict[str, dict[str, Any]] = {
    "anilist": _manifest(
        name="AniList",
        media_types=("anime", "manga"),
        capabilities=("poll", "backfill", "has_ratings", "has_credits"),
        acquisition="api",
        schema_version=2,
        interval_seconds=3600,
        config_schema=_object_schema(
            {
                "username": _anilist_username,
                "token": _anilist_token,
                "api_url": _anilist_api_url,
            },
            required=["username"],
        ),
        rating_scales=(
            _scale("anilist-100", kind="linear", minimum="1", maximum="100", step="1"),
            _scale("anilist-10-decimal", kind="linear", minimum="0.1", maximum="10", step="0.1"),
            _scale(
                "anilist-10",
                kind="ordinal",
                minimum="0",
                maximum="10",
                step="1",
                labels={str(value): value * 10 for value in range(11)},
            ),
            _scale("anilist-5", kind="linear", minimum="1", maximum="5", step="1"),
            _scale(
                "anilist-3",
                kind="ordinal",
                minimum="1",
                maximum="3",
                step="1",
                labels={"1": 0, "2": 50, "3": 100},
            ),
        ),
    ),
    "fixture": _manifest(
        name="Fixture",
        media_types=("book", "film", "tv"),
        capabilities=(
            "poll",
            "backfill",
            "file_import",
            "has_ratings",
            "has_reviews",
            "has_credits",
        ),
        acquisition="export",
        schema_version=1,
        interval_seconds=3600,
        config_schema=_object_schema({"path": _fixture_path}, required=["path"]),
        rating_scales=(
            _scale("fixture-stars-5", kind="linear", minimum="0.5", maximum="5", step="0.5"),
        ),
    ),
    "goodreads": _manifest(
        name="Goodreads",
        media_types=("book",),
        capabilities=("poll", "file_import", "has_ratings", "has_reviews", "has_credits"),
        acquisition="feed",
        schema_version=3,
        interval_seconds=86400,
        config_schema=_object_schema(
            {"profile_url": _goodreads_profile, "export_path": _goodreads_export}
        ),
        rating_scales=(
            _scale("goodreads-5-star", kind="linear", minimum="1", maximum="5", step="1"),
        ),
    ),
    "koito": _manifest(
        name="Koito",
        media_types=("track",),
        capabilities=("poll", "backfill", "has_credits"),
        acquisition="api",
        schema_version=1,
        interval_seconds=300,
        config_schema=_object_schema(
            {"base_url": _koito_base_url, "api_key": _koito_api_key},
            required=["base_url", "api_key"],
        ),
    ),
    "letterboxd": _manifest(
        name="Letterboxd",
        media_types=("film", "tv"),
        capabilities=("poll", "file_import", "has_ratings", "has_reviews"),
        acquisition="feed",
        schema_version=1,
        interval_seconds=21600,
        config_schema=_object_schema(
            {
                "username": _letterboxd_username,
                "rss_url": _letterboxd_rss,
                "export_path": _letterboxd_export,
            }
        ),
        rating_scales=(
            _scale(
                "letterboxd-5-star-halves",
                kind="linear",
                minimum="0.5",
                maximum="5",
                step="0.5",
            ),
        ),
        import_inference="letterboxd_rss_username",
    ),
    "listenbrainz": _manifest(
        name="ListenBrainz",
        media_types=("track",),
        capabilities=("poll", "backfill", "has_credits"),
        acquisition="api",
        schema_version=1,
        interval_seconds=900,
        config_schema=_object_schema(
            {
                "username": _listenbrainz_username,
                "token": _listenbrainz_token,
                "base_url": _listenbrainz_base,
            },
            required=["username", "token"],
        ),
    ),
}
