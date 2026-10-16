"""YouTube Music's authenticated history snapshot stays stable across imports."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

import aggregato.providers.youtube_music as youtube_music_module
from aggregato.db.engine import create_engine
from aggregato.db.schema import entries, metadata, provider_items, sync_runs, works
from aggregato.db.search import create_search_index
from aggregato.domain.enums import FetchMode, LoggedPrecision, MediaType, Role
from aggregato.domain.models import Checkpoint, RawRecord
from aggregato.ingest.writer import WriteContext, write_batches
from aggregato.providers.base import Provider, ProviderContext
from aggregato.providers.errors import AuthError, BlockedError, RateLimited, StructureChangedError
from aggregato.providers.registry import load_provider

FIXTURES = Path(__file__).parents[1] / "fixtures" / "youtube_music"
DATE_HEADER = "Thu, 15 Oct 2026 10:00:00 GMT"
VIDEO_ID = "abcdefghijk"
CHANNEL_ID = "UC0000000000000000000001"
ALBUM_ID = "MPRE0000000000000001"
TODAY_RAW_ID = f"{VIDEO_ID}:2026-10-15"
YESTERDAY_RAW_ID = f"{VIDEO_ID}:2026-10-14"
SECOND_TODAY_RAW_ID = "lmnopqrstuv:2026-10-15"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _provider() -> Provider:
    return load_provider("youtube_music")


def _context(
    path: Path,
    *,
    timezone: str = "UTC",
    date_header: str = DATE_HEADER,
    requests: list[httpx2.Request] | None = None,
    like_status: str | None = None,
    section_label: str | None = None,
) -> ProviderContext:
    provider = _provider()
    cookie = (
        "__Secure-3PAPISID=fixture-invalid"
        if path.name == "credentials-invalid.json"
        else "__Secure-3PAPISID=fixture-session"
    )
    config = provider.config_model(cookie=cookie, auth_user="0", timezone=timezone)

    def respond(request: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(request)
        if path.name == "blocked.html":
            return httpx2.Response(
                403,
                content=path.read_bytes(),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if path.name == "rate-limited.json":
            return httpx2.Response(
                429,
                json=json.loads(path.read_text()),
                headers={"retry-after": "120"},
            )
        if path.name == "credentials-invalid.json":
            return httpx2.Response(401, json=json.loads(path.read_text()))
        body = json.loads(path.read_text())
        if like_status is not None:
            shelf = body["contents"]["singleColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"][
                "content"
            ]["sectionListRenderer"]["contents"][0]["musicShelfRenderer"]
            shelf["contents"][0]["musicResponsiveListItemRenderer"]["menu"]["menuRenderer"][
                "topLevelButtons"
            ][0]["likeButtonRenderer"]["likeStatus"] = like_status
        if section_label is not None:
            shelf = body["contents"]["singleColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"][
                "content"
            ]["sectionListRenderer"]["contents"][0]["musicShelfRenderer"]
            shelf["title"] = {"runs": [{"text": section_label}]}
        return httpx2.Response(200, json=body, headers={"date": date_header})

    return ProviderContext(
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        config=config,
        secrets={},
        log=logging.getLogger(__name__),
        state={},
    )


async def _records(provider: Provider, ctx: ProviderContext) -> list[RawRecord]:
    result: list[RawRecord] = []
    async for item in provider.fetch(ctx, None, FetchMode.FULL):
        if isinstance(item, RawRecord):
            result.append(item)
        else:
            assert isinstance(item, Checkpoint)
    return result


async def test_fetch_uses_the_authenticated_history_browse_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    requests: list[httpx2.Request] = []
    ctx = _context(FIXTURES / "page-1.json", requests=requests)
    timestamps = iter((1_700_000_000, 1_700_000_060))
    monkeypatch.setattr(
        youtube_music_module,
        "time",
        SimpleNamespace(time=lambda: next(timestamps)),
    )
    try:
        records = await _records(provider, ctx)
        await _records(provider, ctx)
    finally:
        await ctx.http.aclose()

    assert len(requests) == 2
    request = requests[0]
    assert request.method == "POST"
    assert request.url.host == "music.youtube.com"
    assert request.url.path == "/youtubei/v1/browse"
    assert request.url.params.get("alt") == "json"
    assert request.url.params.get("key") == "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"
    assert request.headers["x-goog-authuser"] == "0"
    assert request.headers["origin"] == "https://music.youtube.com"
    assert "__Secure-3PAPISID=fixture-session" in request.headers["cookie"]
    for signed_request, timestamp in zip(requests, (1_700_000_000, 1_700_000_060), strict=True):
        expected_hash = hashlib.sha1(
            f"{timestamp} fixture-session https://music.youtube.com".encode(),
            usedforsecurity=False,
        ).hexdigest()
        assert signed_request.headers["authorization"] == (
            f"SAPISIDHASH {timestamp}_{expected_hash}"
        )
    assert requests[0].headers["authorization"] != requests[1].headers["authorization"]
    body = json.loads(request.content)
    assert body["browseId"] == "FEmusic_history"
    assert body["context"]["client"]["clientName"] == "WEB_REMIX"
    assert body["context"]["client"]["hl"]

    # The deleted item and explicit podcast row are recognized but do not become track listens.
    assert [record.native_id for record in records] == [
        TODAY_RAW_ID,
        SECOND_TODAY_RAW_ID,
        TODAY_RAW_ID,
        YESTERDAY_RAW_ID,
    ]


async def test_normalize_preserves_ids_without_guessing_performer_role() -> None:
    provider = _provider()
    ctx = _context(FIXTURES / "page-1.json")
    try:
        records = await _records(provider, ctx)
    finally:
        await ctx.http.aclose()
    song = next(record for record in records if record.native_id == TODAY_RAW_ID)
    batch = provider.normalize(song)
    stored_item = json.dumps(song.payload)

    assert batch.work.media_type is MediaType.TRACK
    assert batch.work.title == "Watched Song"
    assert batch.work.metadata["like_status"] == "INDIFFERENT"
    assert batch.opinions == []
    assert batch.entries[0].logged_precision is LoggedPrecision.DAY
    assert batch.entries[0].native_id is None
    assert ("youtube_video", VIDEO_ID) in {
        (external.namespace, external.value) for external in batch.external_ids
    }
    assert ("youtube_music", f"video:{VIDEO_ID}:album:{ALBUM_ID}") in {
        (external.namespace, external.value) for external in batch.external_ids
    }
    assert ("youtube_channel", CHANNEL_ID) in {
        (external.namespace, external.value) for external in batch.creator_external_ids
    }
    assert batch.credits
    assert {credit.role for credit in batch.credits} == {Role.OTHER}
    assert all(credit.role_raw for credit in batch.credits)
    assert "synthetic-feedback-token" not in stored_item
    assert "feedbackToken" not in stored_item
    assert "synthetic-tracking-params" not in stored_item
    assert "trackingParams" not in stored_item


async def test_plain_text_artist_is_kept_without_a_creator_identifier() -> None:
    provider = _provider()
    ctx = _context(FIXTURES / "page-1.json")
    try:
        records = await _records(provider, ctx)
    finally:
        await ctx.http.aclose()

    raw = next(record for record in records if record.native_id == SECOND_TODAY_RAW_ID)
    batch = provider.normalize(raw)

    assert [(credit.creator_name, credit.role, credit.role_raw) for credit in batch.credits] == [
        ("Example Channel", Role.OTHER, "text")
    ]
    assert batch.creator_external_ids == []


@pytest.mark.parametrize(
    ("like_status", "expected_is_liked"),
    [("LIKE", True), ("DISLIKE", False), ("INDIFFERENT", None)],
)
async def test_menu_like_status_maps_only_explicit_like_and_dislike(
    like_status: str, expected_is_liked: bool | None
) -> None:
    provider = _provider()
    ctx = _context(FIXTURES / "page-1.json", like_status=like_status)
    try:
        records = await _records(provider, ctx)
    finally:
        await ctx.http.aclose()

    raw = next(record for record in records if record.native_id == TODAY_RAW_ID)
    batch = provider.normalize(raw)

    assert batch.work.metadata["like_status"] == like_status
    if expected_is_liked is None:
        assert batch.opinions == []
    else:
        assert [opinion.is_liked for opinion in batch.opinions] == [expected_is_liked]


async def test_relative_day_labels_use_response_date_and_configured_timezone() -> None:
    provider = _provider()
    date_header = "Thu, 15 Oct 2026 01:00:00 GMT"
    ctx = _context(
        FIXTURES / "page-1.json",
        timezone="America/Los_Angeles",
        date_header=date_header,
    )
    try:
        records = await _records(provider, ctx)
    finally:
        await ctx.http.aclose()

    today = next(
        record
        for record in records
        if record.native_id.startswith(f"{VIDEO_ID}:") and record.payload["played_label"] == "Today"
    )
    assert provider.normalize(today).entries[0].logged_at == datetime(
        2026, 10, 14, 7, 0, tzinfo=UTC
    )


async def test_leap_day_label_resolves_against_the_response_date() -> None:
    provider = _provider()
    ctx = _context(
        FIXTURES / "page-1.json",
        date_header="Fri, 01 Mar 2024 10:00:00 GMT",
        section_label="February 29",
    )
    try:
        records = await _records(provider, ctx)
    finally:
        await ctx.http.aclose()

    leap_day = next(record for record in records if record.native_id == f"{VIDEO_ID}:2024-02-29")
    assert leap_day.payload["logged_day"] == "2024-02-29"
    assert provider.normalize(leap_day).entries[0].logged_at == datetime(2024, 2, 29, tzinfo=UTC)


async def test_valid_empty_history_is_empty_and_changed_envelopes_fail() -> None:
    provider = _provider()
    empty_ctx = _context(FIXTURES / "empty-history.json")
    try:
        assert await _records(provider, empty_ctx) == []
    finally:
        await empty_ctx.http.aclose()

    changed_ctx = _context(FIXTURES / "structure-changed.json")
    try:
        with pytest.raises(StructureChangedError):
            await _records(provider, changed_ctx)
    finally:
        await changed_ctx.http.aclose()


async def test_auth_block_and_rate_limit_responses_are_classified() -> None:
    provider = _provider()
    cases = [
        ("credentials-invalid.json", AuthError),
        ("blocked.html", BlockedError),
        ("rate-limited.json", RateLimited),
    ]
    for name, error in cases:
        ctx = _context(FIXTURES / name)
        try:
            with pytest.raises(error) as caught:
                await _records(provider, ctx)
            if error is RateLimited:
                assert caught.value.retry_after == 120
        finally:
            await ctx.http.aclose()


async def test_check_reports_actionable_valid_and_rejected_session_results() -> None:
    provider = _provider()
    valid_ctx = _context(FIXTURES / "page-1.json")
    invalid_ctx = _context(FIXTURES / "credentials-invalid.json")
    try:
        ok = await provider.check(valid_ctx)
        bad = await provider.check(invalid_ctx)
    finally:
        await valid_ctx.http.aclose()
        await invalid_ctx.http.aclose()

    assert ok.ok
    assert not bad.ok
    assert bad.error_class == "auth"
    assert bad.detail and "cookie" in bad.detail.casefold()


def test_invalid_timezone_is_rejected_as_configuration_error() -> None:
    provider = _provider()
    with pytest.raises(ValidationError, match="timezone"):
        provider.config_model(
            cookie="__Secure-3PAPISID=fixture-session",
            auth_user="0",
            timezone="Mars/Phobos",
        )


def _fts5_available() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()
    return True


@pytest.mark.skipif(
    not _fts5_available(), reason="this SQLite build has no FTS5; the writer maintains that index"
)
async def test_snapshot_reimport_deduplicates_same_day_and_keeps_distinct_play_dates(
    tmp_path: Path,
) -> None:
    provider = _provider()
    ctx = _context(FIXTURES / "page-1.json")
    try:
        records = await _records(provider, ctx)
    finally:
        await ctx.http.aclose()
    normalized = [(record, provider.normalize(record)) for record in records]

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'youtube-music.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await create_search_index(connection)
            for run_id, started_at in ((1, NOW), (2, NOW + timedelta(days=1))):
                await connection.execute(
                    sync_runs.insert().values(
                        id=run_id,
                        provider_id=provider.id,
                        lineage_id=uuid.uuid4(),
                        attempt=1,
                        mode="full",
                        status="running",
                        started_at=started_at,
                    )
                )

            def write_context(sync_run_id: int, now: datetime) -> WriteContext:
                return WriteContext(
                    provider_id=provider.id,
                    sync_run_id=sync_run_id,
                    schema_version=provider.schema_version,
                    now=now,
                    rating_scales={scale.id: scale for scale in provider.rating_scales},
                )

            await write_batches(connection, write_context(1, NOW), normalized)
            first_count = int(
                (await connection.execute(select(func.count()).select_from(entries))).scalar_one()
            )
            assert first_count == 3
            assert (
                int(
                    (
                        await connection.execute(select(func.count()).select_from(provider_items))
                    ).scalar_one()
                )
                == 3
            )
            assert (
                int(
                    (await connection.execute(select(func.count()).select_from(works))).scalar_one()
                )
                == 2
            ), "tracks from one album must remain distinct works"

            dates = (
                (
                    await connection.execute(
                        select(entries.c.logged_at)
                        .join(provider_items, entries.c.provider_item_id == provider_items.c.id)
                        .where(
                            provider_items.c.provider_id == provider.id,
                            provider_items.c.native_id.in_((TODAY_RAW_ID, YESTERDAY_RAW_ID)),
                            entries.c.deleted_at.is_(None),
                        )
                        .order_by(entries.c.logged_at)
                    )
                )
                .scalars()
                .all()
            )
            assert [value.replace(tzinfo=UTC) for value in dates] == [
                datetime(2026, 10, 14, tzinfo=UTC),
                datetime(2026, 10, 15, tzinfo=UTC),
            ]

            stored_rows = (
                await connection.execute(
                    select(provider_items.c.native_id, provider_items.c.raw_payload)
                    .where(provider_items.c.provider_id == provider.id)
                    .order_by(provider_items.c.native_id)
                )
            ).all()
            replayed = {
                native_id: provider.normalize(RawRecord(native_id=native_id, payload=raw_payload))
                .entries[0]
                .logged_at.replace(tzinfo=UTC)
                for native_id, raw_payload in stored_rows
            }
            assert replayed == {
                TODAY_RAW_ID: datetime(2026, 10, 15, tzinfo=UTC),
                YESTERDAY_RAW_ID: datetime(2026, 10, 14, tzinfo=UTC),
                SECOND_TODAY_RAW_ID: datetime(2026, 10, 15, tzinfo=UTC),
            }

            await write_batches(
                connection,
                write_context(2, NOW + timedelta(days=1)),
                list(reversed(normalized)),
            )
            second_count = int(
                (await connection.execute(select(func.count()).select_from(entries))).scalar_one()
            )
            assert second_count == first_count
            assert (
                int(
                    (
                        await connection.execute(select(func.count()).select_from(provider_items))
                    ).scalar_one()
                )
                == 3
            )
    finally:
        await engine.dispose()
