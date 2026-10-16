"""Serializd's synthetic wire cases exercise replay and failure boundaries offline."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx2
import pytest

from aggregato.domain.enums import EntryKind, FetchMode, LoggedPrecision, ReviewFormat
from aggregato.domain.models import RawRecord
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import (
    AuthError,
    ProviderError,
    StructureChangedError,
    TransportError,
)
from aggregato.providers.serializd import SerializdConfig, provider
from tests.conformance.conftest import REGISTERED, build_ctx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from httpx2 import AsyncClient


def _serializd_fixture(fixtures_dir: Path, name: str) -> Path:
    return fixtures_dir / "serializd" / name


def _row(fixtures_dir: Path, page: str, index: int = 0) -> dict[str, object]:
    response = json.loads(_serializd_fixture(fixtures_dir, page).read_text())
    return response["reviews"][index]


def _context(client: httpx2.AsyncClient, *, username: str = "fixture-user") -> ProviderContext:
    return ProviderContext(
        http=cast("AsyncClient", client),
        config=SerializdConfig(username=username, token="fixture-token"),
        secrets={},
        log=logging.getLogger("test.serializd"),
        state={},
    )


async def _fetch_full(ctx: ProviderContext) -> list[object]:
    return [item async for item in provider.fetch(ctx, None, FetchMode.FULL)]


def test_normalize_keeps_episode_opinion_subject_and_review_details(fixtures_dir: Path) -> None:
    payload = _row(fixtures_dir, "page-1.json", 1)
    raw = RawRecord(native_id=str(payload["id"]), payload=payload)

    batch = provider.normalize(raw)

    assert len(batch.entries) == 1
    entry = batch.entries[0]
    assert entry.subject_ref is not None
    assert entry.subject_ref.model_dump(exclude_none=True) == {"season": 1, "episode": 1}
    assert entry.kind is EntryKind.REWATCH
    assert entry.logged_at.isoformat() == "2025-02-11T00:00:00+00:00"
    assert entry.logged_precision is LoggedPrecision.DAY
    assert len(batch.opinions) == 1
    opinion = batch.opinions[0]
    assert opinion.subject_ref == entry.subject_ref
    assert opinion.rating_raw == 8
    assert opinion.rating_scale_id == "serializd-10-point"
    assert opinion.review_text is None
    assert opinion.contains_spoilers is False
    assert batch.work.image_url == "https://image.tmdb.org/t/p/w500/example-banner.webp"


def test_rating_only_row_keeps_opinion_without_inventing_a_watch(fixtures_dir: Path) -> None:
    payload = _row(fixtures_dir, "page-1.json", 0)
    raw = RawRecord(native_id=str(payload["id"]), payload=payload)

    batch = provider.normalize(raw)

    assert batch.entries == []
    assert batch.retracts_entries is True
    assert len(batch.opinions) == 1
    opinion = batch.opinions[0]
    assert opinion.rating_raw == 9
    assert opinion.review_text == "  A synthetic fixture review for a season two episode. \n"
    assert opinion.review_format is ReviewFormat.PLAIN
    assert opinion.contains_spoilers is True
    assert opinion.subject_ref is not None
    assert opinion.subject_ref.model_dump(exclude_none=True) == {"season": 2, "episode": 3}


def test_normalize_rejects_boolean_rating(fixtures_dir: Path) -> None:
    payload = _row(fixtures_dir, "page-1.json", 0)
    payload["rating"] = False

    with pytest.raises(ValueError, match="rating must be an integer"):
        provider.normalize(RawRecord(native_id=str(payload["id"]), payload=payload))


def test_normalize_rejects_missing_diary_kind(fixtures_dir: Path) -> None:
    payload = _row(fixtures_dir, "page-1.json", 1)
    payload.pop("isLog")

    with pytest.raises(StructureChangedError, match="isLog"):
        provider.normalize(RawRecord(native_id=str(payload["id"]), payload=payload))


def test_normalize_rejects_unrepresentable_special_season_zero(fixtures_dir: Path) -> None:
    payload = _row(fixtures_dir, "page-1.json", 1)
    matching_season_id = payload["seasonId"]
    for season in payload["showSeasons"]:
        if season["id"] == matching_season_id:
            season["seasonNumber"] = 0
            break

    with pytest.raises(ValueError, match="season zero"):
        provider.normalize(RawRecord(native_id=str(payload["id"]), payload=payload))


def test_normalize_emits_show_and_all_season_identifiers(fixtures_dir: Path) -> None:
    payload = _row(fixtures_dir, "page-1.json", 0)
    batch = provider.normalize(RawRecord(native_id=str(payload["id"]), payload=payload))

    identifiers = {(value.namespace, value.value) for value in batch.external_ids}
    assert ("serializd", "66573") in identifiers
    assert {("serializd_season", str(season["id"])) for season in payload["showSeasons"]} <= (
        identifiers
    )
    assert ("serializd", "910001") not in identifiers


@pytest.mark.parametrize(
    "fixture_name",
    (
        "page-2-empty.json",
        "page-2-total-pages-changed.json",
        "page-2-duplicate-id.json",
    ),
)
async def test_pagination_truncation_or_drift_is_a_transport_failure(
    fixtures_dir: Path,
    fixture_name: str,
) -> None:
    registration = next(row for row in REGISTERED if row.provider_id == "serializd")
    ctx = build_ctx(registration, _serializd_fixture(fixtures_dir, fixture_name))
    try:
        with pytest.raises(TransportError):
            await _fetch_full(ctx)
    finally:
        await ctx.http.aclose()


@pytest.mark.parametrize(
    ("fixture_name", "error"),
    (
        ("http-error.json", TransportError),
        ("malformed.json", StructureChangedError),
    ),
)
async def test_http_and_malformed_success_responses_are_classified(
    fixtures_dir: Path,
    fixture_name: str,
    error: type[Exception],
) -> None:
    registration = next(row for row in REGISTERED if row.provider_id == "serializd")
    ctx = build_ctx(registration, _serializd_fixture(fixtures_dir, fixture_name))
    try:
        with pytest.raises(error):
            await _fetch_full(ctx)
    finally:
        await ctx.http.aclose()


async def test_identity_mismatch_stops_before_the_diary_request() -> None:
    paths: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/user_information":
            return httpx2.Response(200, json={"username": "another-user"})
        raise AssertionError("Serializd diary was requested for a mismatched account")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        ctx = _context(client)
        with pytest.raises(AuthError, match="does not match"):
            await _fetch_full(ctx)

    assert paths == ["/api/user_information"]


async def test_import_mode_is_rejected_without_http() -> None:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise AssertionError("Serializd import mode must not make an HTTP request")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        ctx = _context(client)
        with pytest.raises(ProviderError, match="does not support file import"):
            async for _item in provider.fetch(ctx, None, FetchMode.IMPORT):
                pass

    assert requests == []
