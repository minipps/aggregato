"""AniList normalization preserves discrete scores and both staff and studio identities."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx

from aggregato.domain.enums import CreatorKind, FetchMode, Role, ScaleKind
from aggregato.domain.models import RawRecord
from aggregato.providers.anilist import AniListConfig, AniListProvider, _headers
from aggregato.providers.base import ProviderContext
from aggregato.sync.child import _politeness_policy


def test_anilist_fixture_has_season_staff_studio_and_ordinal_rating() -> None:
    payload = json.loads((Path("tests/fixtures/anilist/records.json")).read_text())
    item = payload["data"]["MediaListCollection"]["lists"][0]["entries"][0]
    batch = AniListProvider().normalize(type("Raw", (), {"native_id": "101", "payload": item})())

    assert batch.work.media_type == "anime_series"
    assert batch.opinions[0].rating_raw == Decimal(8)
    assert {credit.role for credit in batch.credits} == {Role.DIRECTOR, Role.STUDIO}
    assert {credit.creator_kind for credit in batch.credits} == {
        CreatorKind.PERSON,
        CreatorKind.STUDIO,
    }
    assert {identifier.value for identifier in batch.creator_external_ids} == {"10", "11"}
    scale = AniListProvider().rating_scales[0]
    assert scale.kind is ScaleKind.ORDINAL
    assert scale.labels is not None and scale.labels["8"] == 80


async def test_anilist_fetches_anime_and_manga_collections_separately() -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        requests.append(dict(variables))
        media_type = variables["type"]
        item = {"id": media_type, "media": {"type": media_type}}
        return httpx.Response(
            200, json={"data": {"MediaListCollection": {"lists": [{"entries": [item]}]}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        context = ProviderContext(
            http=http,  # type: ignore[arg-type]
            config=AniListProvider.config_model(username="mini"),
            secrets={},
            log=logging.getLogger(__name__),
            state={},
        )
        records = [
            item
            async for item in AniListProvider().fetch(context, None, FetchMode.INCREMENTAL)
            if isinstance(item, RawRecord)
        ]

    assert requests == [{"name": "mini", "type": "ANIME"}, {"name": "mini", "type": "MANGA"}]
    assert [record.native_id for record in records] == ["ANIME", "MANGA"]


def test_anilist_schedule_interval_is_not_an_http_request_delay() -> None:
    """An hourly poll still needs its Anime and Manga requests to run back-to-back."""
    assert _politeness_policy(AniListProvider()).effective_interval_seconds == 0.1


def test_anilist_omits_an_empty_optional_token_header() -> None:
    assert _headers(AniListConfig(username="mini", token="")) == {}
