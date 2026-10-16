"""Backloggd's rolling public reviews feed stays review-only and offline."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from aggregato.domain.enums import (
    Acquisition,
    Capability,
    ErrorClass,
    FetchMode,
    MediaType,
    ReviewFormat,
)
from aggregato.domain.models import Checkpoint, Cursor, RawRecord
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import (
    BlockedError,
    RateLimited,
    StructureChangedError,
)
from aggregato.providers.registry import load_provider
from tests.conformance.conftest import REGISTERED, build_ctx, fetch_all, only_records

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "backloggd"
_REGISTRATION = next(
    registration for registration in REGISTERED if registration.provider_id == "backloggd"
)
_EXPECTED_REVIEWS = (
    ("Portal 2", 2011, 9, "2026-10-15T20:32:31+00:00"),
    ("Final Fantasy XVI", 2023, 7, "2026-10-08T13:15:35+00:00"),
    ("Back to the Future: The Game", 2010, 6, "2026-09-05T14:07:19+00:00"),
    ("Nine Sols", 2024, 9, "2026-09-01T17:48:52+00:00"),
    ("Marvel's Guardians of the Galaxy", 2021, 7, "2026-08-19T18:50:40+00:00"),
    ("Sekiro: Shadows Die Twice - Game of the Year Edition", 2020, 10, "2026-08-12T19:20:11+00:00"),
    ("Portal", 2007, 8, "2026-07-31T14:42:08+00:00"),
    ("The Wolf Among Us", 2013, 8, "2026-07-29T18:24:05+00:00"),
    ("Super Mario Odyssey", 2017, 7, "2026-07-27T12:24:07+00:00"),
    ("Hades", 2020, 8, "2026-06-29T14:47:58+00:00"),
    ("Kentucky Route Zero: TV Edition", 2020, 7, "2026-06-23T11:56:41+00:00"),
    ("Souldiers", 2022, 5, "2026-05-15T13:07:10+00:00"),
    ("Marvel's Spider-Man 2", 2023, 7, "2026-05-15T12:48:42+00:00"),
    ("The Legend of Zelda: Breath of the Wild", 2017, 10, "2026-05-09T15:55:39+00:00"),
    ("Octopath Traveler II", 2023, 8, "2026-04-13T12:19:01+00:00"),
    ("Mega Man 11", 2018, 8, "2026-03-30T21:47:47+00:00"),
    ("Bloodborne", 2015, 10, "2026-02-13T20:46:06+00:00"),
    ("Hot Wheels Unleashed 2: Turbocharged", 2023, 6, "2026-01-04T18:02:44+00:00"),
    ("Silent Hill 2", 2024, 8, "2025-12-08T22:03:05+00:00"),
    ("Thank Goodness You're Here!", 2024, 7, "2025-12-02T13:40:01+00:00"),
)


async def _records(filename: str) -> tuple[list[RawRecord], list[Checkpoint]]:
    provider = load_provider("backloggd")
    ctx = build_ctx(_REGISTRATION, _FIXTURES / filename)
    try:
        items = await fetch_all(provider, ctx, None, FetchMode.FULL)
    finally:
        await ctx.http.aclose()
    return only_records(items), [item for item in items if isinstance(item, Checkpoint)]


async def test_feed_yields_review_guids_as_native_ids_without_cursor_claims() -> None:
    provider = load_provider("backloggd")
    records, checkpoints = await _records("reviews.rss")

    assert provider.acquisition is Acquisition.FEED
    assert provider.media_types == {MediaType.GAME}
    assert provider.capabilities == {
        Capability.POLL,
        Capability.HAS_RATINGS,
        Capability.HAS_REVIEWS,
    }
    assert [record.native_id for record in records] == [
        f"backloggd-review-{9_000_000 + index:07d}" for index in range(1, 21)
    ]
    assert [record.payload["guid"] for record in records] == [
        record.native_id for record in records
    ]
    assert checkpoints == []


async def test_rolling_feed_is_fetched_again_even_with_prior_snapshot_cursor() -> None:
    provider = load_provider("backloggd")
    ctx = build_ctx(_REGISTRATION, _FIXTURES / "reviews.rss")
    try:
        first = await fetch_all(provider, ctx, None, FetchMode.INCREMENTAL)
        second = await fetch_all(
            provider,
            ctx,
            Cursor(state={"feed_complete": True}),
            FetchMode.INCREMENTAL,
        )
    finally:
        await ctx.http.aclose()

    first_ids = [record.native_id for record in only_records(first)]
    second_ids = [record.native_id for record in only_records(second)]
    assert len(first_ids) == 20
    assert second_ids == first_ids


async def test_normalize_keeps_rating_and_review_time_without_inventing_play_history() -> None:
    provider = load_provider("backloggd")
    records, _ = await _records("reviews.rss")
    batch = provider.normalize(records[0])

    assert batch.work.media_type is MediaType.GAME
    # The recorded first title includes the literal half-star glyph `½`.
    assert batch.work.title == "Portal 2"
    assert batch.work.release_year == 2011
    assert batch.entries == []
    assert batch.external_ids == []
    assert len(batch.opinions) == 1

    opinion = batch.opinions[0]
    assert opinion.rating_raw == Decimal("9")
    assert opinion.rating_scale_id in {scale.id for scale in provider.rating_scales}
    scale = next(scale for scale in provider.rating_scales if scale.id == opinion.rating_scale_id)
    assert scale.max_value == Decimal("10")
    assert opinion.review_text is not None
    assert opinion.review_format is ReviewFormat.PLAIN
    assert opinion.authored_at == datetime(2026, 10, 15, 20, 32, 31, tzinfo=UTC)


async def test_all_twenty_recorded_reviews_match_title_rating_and_authored_date() -> None:
    provider = load_provider("backloggd")
    records, _ = await _records("reviews.rss")
    assert len(records) == len(_EXPECTED_REVIEWS) == 20

    for raw, (title, year, rating, authored_at) in zip(records, _EXPECTED_REVIEWS, strict=True):
        batch = provider.normalize(raw)
        assert batch.work.title == title
        assert batch.work.release_year == year
        assert batch.entries == []
        assert len(batch.opinions) == 1
        opinion = batch.opinions[0]
        assert opinion.rating_raw == Decimal(rating)
        assert opinion.authored_at == datetime.fromisoformat(authored_at)


@pytest.mark.parametrize("rating", ["NaN", "Infinity", "-Infinity", "11", "not-a-number"])
async def test_normalize_rejects_invalid_and_nonfinite_ratings(rating: str) -> None:
    provider = load_provider("backloggd")
    records, _ = await _records("reviews.rss")
    payload = dict(records[0].payload)
    payload["user_rating"] = rating

    with pytest.raises(ValueError):
        provider.normalize(RawRecord(native_id=records[0].native_id, payload=payload))


@pytest.mark.parametrize("field", ["title", "pub_date"])
async def test_normalize_rejects_missing_required_review_fields(field: str) -> None:
    provider = load_provider("backloggd")
    records, _ = await _records("reviews.rss")
    payload = dict(records[0].payload)
    payload.pop(field)

    with pytest.raises(StructureChangedError):
        provider.normalize(RawRecord(native_id=records[0].native_id, payload=payload))


async def test_normalize_rejects_an_invalid_review_date() -> None:
    provider = load_provider("backloggd")
    records, _ = await _records("reviews.rss")
    payload = dict(records[0].payload)
    payload["pub_date"] = "not a date"

    with pytest.raises(ValueError, match="pubDate is not a valid RSS date"):
        provider.normalize(RawRecord(native_id=records[0].native_id, payload=payload))


async def test_valid_empty_feed_is_empty_and_changed_item_structure_fails() -> None:
    records, checkpoints = await _records("empty.rss")
    assert records == []
    assert checkpoints == []

    provider = load_provider("backloggd")
    ctx = build_ctx(_REGISTRATION, _FIXTURES / "structure-changed.rss")
    try:
        with pytest.raises(StructureChangedError):
            await fetch_all(provider, ctx, None, FetchMode.FULL)
    finally:
        await ctx.http.aclose()


@pytest.mark.parametrize(
    "fixture",
    [
        "missing-channel-title.rss",
        "missing-channel-link.rss",
        "missing-channel-description.rss",
        "entry-shape-changed.rss",
    ],
)
async def test_changed_envelope_or_entry_shape_fails_closed(fixture: str) -> None:
    provider = load_provider("backloggd")
    ctx = build_ctx(_REGISTRATION, _FIXTURES / fixture)
    try:
        with pytest.raises(StructureChangedError):
            await fetch_all(provider, ctx, None, FetchMode.FULL)
    finally:
        await ctx.http.aclose()


async def test_unknown_item_xml_is_retained_for_normalization_replay() -> None:
    records, _ = await _records("unknown-item-extension.rss")
    assert len(records) == 1
    source_xml = records[0].payload["source_xml"]
    assert isinstance(source_xml, str)
    assert "future_game_id" in source_xml
    assert "future-value-001" in source_xml


async def test_review_text_with_challenge_words_is_not_a_block_page() -> None:
    provider = load_provider("backloggd")
    records, _ = await _records("review-text-challenge-words.rss")
    assert len(records) == 1
    opinion = provider.normalize(records[0]).opinions[0]
    assert opinion.review_text is not None
    assert "CAPTCHA" in opinion.review_text
    assert "/login" in opinion.review_text


async def test_check_classifies_transport_exceptions() -> None:
    provider = load_provider("backloggd")
    config = provider.config_model(**_REGISTRATION.config(_FIXTURES / "reviews.rss"))

    def respond(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("offline fixture transport", request=request)

    ctx = ProviderContext(
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        config=config,
        secrets={},
        log=logging.getLogger(__name__),
        state={},
    )
    try:
        result = await provider.check(ctx)
    finally:
        await ctx.http.aclose()

    assert not result.ok
    assert result.error_class is ErrorClass.TRANSPORT


@pytest.mark.parametrize(
    ("fixture", "error", "retry_after"),
    [
        ("blocked.html", BlockedError, None),
        ("blocked-200.html", BlockedError, None),
        ("rate-limited.txt", RateLimited, 120),
    ],
)
async def test_fetch_stops_on_a_challenge_or_honors_retry_after(
    fixture: str,
    error: type[BlockedError] | type[RateLimited],
    retry_after: float | None,
) -> None:
    provider = load_provider("backloggd")
    ctx = build_ctx(_REGISTRATION, _FIXTURES / fixture)
    try:
        with pytest.raises(error) as raised:
            await fetch_all(provider, ctx, None, FetchMode.FULL)
    finally:
        await ctx.http.aclose()

    if isinstance(raised.value, RateLimited):
        assert raised.value.retry_after == retry_after
