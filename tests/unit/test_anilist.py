"""AniList normalization preserves discrete scores and both staff and studio identities."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from aggregato.domain.enums import CreatorKind, Role, ScaleKind
from aggregato.providers.anilist import AniListProvider


def test_anilist_fixture_has_season_staff_studio_and_ordinal_rating() -> None:
    payload = json.loads((Path("tests/fixtures/anilist/records.json")).read_text())
    item = payload["data"]["MediaListCollection"]["lists"][0]["entries"][0]
    batch = AniListProvider().normalize(type("Raw", (), {"native_id": "101", "payload": item})())

    assert batch.work.media_type == "anime_season"
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
