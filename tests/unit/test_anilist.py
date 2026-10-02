"""AniList normalization preserves discrete scores and both staff and studio identities."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx2

from aggregato.domain.enums import CreatorKind, EntryKind, FetchMode, Role, ScaleKind
from aggregato.domain.models import Cursor, NormalizedEntry, RawRecord
from aggregato.domain.ratings import normalize_rating
from aggregato.providers.anilist import (
    _QUERY,
    _SCALES,
    AniListConfig,
    AniListProvider,
    _headers,
)
from aggregato.providers.base import ProviderContext
from aggregato.sync.child import _politeness_policy


def test_anilist_fixture_has_season_staff_studio_and_ordinal_rating() -> None:
    payload = json.loads((Path("tests/fixtures/anilist/records.json")).read_text())
    item = payload["data"]["MediaListCollection"]["lists"][0]["entries"][0]
    batch = AniListProvider().normalize(type("Raw", (), {"native_id": "101", "payload": item})())

    assert batch.work.media_type == "anime"
    assert batch.opinions[0].rating_raw == Decimal(8)
    assert {credit.role for credit in batch.credits} == {Role.DIRECTOR, Role.STUDIO}
    assert {credit.creator_kind for credit in batch.credits} == {
        CreatorKind.PERSON,
        CreatorKind.STUDIO,
    }
    assert {identifier.value for identifier in batch.creator_external_ids} == {"10", "11"}
    assert batch.opinions[0].rating_scale_id == "anilist-10"
    scale = _SCALES["POINT_10"]
    assert scale.kind is ScaleKind.ORDINAL
    assert scale.labels is not None and scale.labels["8"] == 80


async def test_anilist_fetches_anime_and_manga_collections_separately() -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        variables = json.loads(request.content)["variables"]
        requests.append(dict(variables))
        media_type = variables["type"]
        item = {"id": media_type, "media": {"type": media_type}}
        return httpx2.Response(
            200, json={"data": {"MediaListCollection": {"lists": [{"entries": [item]}]}}}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
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


async def test_a_stored_cursor_does_not_silence_later_runs() -> None:
    """The reported symptom: resyncing changed nothing because nothing was fetched.

    This provider used to checkpoint ``{"complete": True}`` and return early on it, so every run
    after the first yielded no records at all — no status change and no new title could ever arrive.
    A mutable list snapshot has to be re-read.
    """
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        media_type = json.loads(request.content)["variables"]["type"]
        item = {"id": media_type, "status": "COMPLETED", "media": {"type": media_type}}
        return httpx2.Response(
            200, json={"data": {"MediaListCollection": {"lists": [{"entries": [item]}]}}}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        context = ProviderContext(
            http=http,
            config=AniListProvider.config_model(username="mini"),
            secrets={},
            log=logging.getLogger(__name__),
            state={},
        )
        stale = Cursor(state={"complete": True})
        records = [
            item
            async for item in AniListProvider().fetch(context, stale, FetchMode.INCREMENTAL)
            if isinstance(item, RawRecord)
        ]

    assert [record.native_id for record in records] == ["ANIME", "MANGA"]
    assert calls == 2


async def test_no_checkpoint_is_yielded_so_no_cursor_goes_stale() -> None:
    """One unpaginated request per media type has no resumable position to record."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": {"MediaListCollection": {"lists": []}}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        context = ProviderContext(
            http=http,
            config=AniListProvider.config_model(username="mini"),
            secrets={},
            log=logging.getLogger(__name__),
            state={},
        )
        items = [item async for item in AniListProvider().fetch(context, None, FetchMode.FULL)]

    assert items == []


def test_anilist_schedule_interval_is_not_an_http_request_delay() -> None:
    """An hourly poll still needs its Anime and Manga requests to run back-to-back."""
    assert _politeness_policy(AniListProvider()).effective_interval_seconds == 0.1


def test_anilist_omits_an_empty_optional_token_header() -> None:
    assert _headers(AniListConfig(username="mini", token="")) == {}


# --- list status -> entry kind -------------------------------------------------------------------


def _normalize(status: str, **fields: object) -> list[NormalizedEntry]:
    item = {
        "id": 7,
        "status": status,
        "updatedAt": 1700000000,
        "media": {"id": 70, "type": "ANIME", "title": {"romaji": "T"}},
        **fields,
    }
    return AniListProvider().normalize(RawRecord(native_id="7", payload=item)).entries


def test_a_plan_to_watch_entry_logs_nothing() -> None:
    """The reported bug: a status the account never acted on was rendered as a watch."""
    assert _normalize("PLANNING", progress=0) == []


def test_status_maps_to_the_kind_it_attests_to() -> None:
    assert [entry.kind for entry in _normalize("COMPLETED", progress=12)] == [EntryKind.WATCH]
    assert [entry.kind for entry in _normalize("REPEATING", progress=12)] == [EntryKind.REWATCH]
    assert [entry.kind for entry in _normalize("DROPPED", progress=2)] == [EntryKind.DROP]


def test_a_manga_completion_is_a_read_not_a_watch() -> None:
    item = {
        "id": 8,
        "status": "COMPLETED",
        "progress": 40,
        "updatedAt": 1700000000,
        "media": {"id": 80, "type": "MANGA", "title": {"romaji": "M"}},
    }
    batch = AniListProvider().normalize(RawRecord(native_id="8", payload=item))
    assert [entry.kind for entry in batch.entries] == [EntryKind.READ]


def test_watching_becomes_progress_carrying_the_episode_count() -> None:
    (entry,) = _normalize("CURRENT", progress=3)
    assert entry.kind is EntryKind.PROGRESS
    assert (entry.progress_value, entry.progress_unit) == (Decimal(3), "episodes")


def test_watching_nothing_yet_states_nothing() -> None:
    """A progress entry must carry a value, and zero episodes is not a logged event."""
    assert _normalize("CURRENT", progress=0) == []
    assert _normalize("PAUSED") == []


def test_an_unrecognized_status_invents_no_event() -> None:
    """Falling back to a watch is how the reported bug read plan-to-watch as history."""
    assert _normalize("SOMETHING_NEW", progress=5) == []


def test_a_stated_plan_retracts_but_a_payload_predating_the_field_does_not() -> None:
    """Replay must not empty the log of everything the older query stored.

    A payload fetched before this provider asked for ``status`` says nothing about the account's
    intent, so re-normalizing it may not withdraw the entry it once produced. A payload that does
    state ``PLANNING`` may.
    """
    stored_before = {
        "id": 9,
        "updatedAt": 1700000000,
        "media": {"id": 90, "type": "ANIME", "title": {"romaji": "Old"}},
    }
    old = AniListProvider().normalize(RawRecord(native_id="9", payload=stored_before))
    assert old.entries == []
    assert old.retracts_entries is False

    planned = AniListProvider().normalize(
        RawRecord(native_id="9", payload=stored_before | {"status": "PLANNING"})
    )
    assert planned.retracts_entries is True


def test_the_query_asks_for_the_fields_the_mapping_reads() -> None:
    """The mapping is silent, not wrong, if ``status`` never arrives."""
    assert "status" in _QUERY
    assert "progress" in _QUERY


def test_the_fixture_covers_the_statuses_that_render_differently() -> None:
    payload = json.loads((Path("tests/fixtures/anilist/records.json")).read_text())
    items = payload["data"]["MediaListCollection"]["lists"][0]["entries"]
    kinds = [
        [
            entry.kind
            for entry in AniListProvider().normalize(RawRecord(native_id="x", payload=i)).entries
        ]
        for i in items
    ]
    assert kinds == [[EntryKind.WATCH], [], [EntryKind.PROGRESS]]


async def test_a_five_point_account_is_rated_on_the_five_point_scale() -> None:
    """The reported bug: every account was declared POINT_10, whatever it had selected.

    A POINT_5 account's 4 stars was read as 4 out of 10 and normalized to 40 — an opinion the
    account never held. AniList reports ``score`` in the account's own format and nowhere in the
    score itself says which, so the format has to be fetched and retained alongside it.
    """

    def respond(request: httpx2.Request) -> httpx2.Response:
        item = {
            "id": 7,
            "score": 4,
            "status": "COMPLETED",
            "updatedAt": 1700000000,
            "media": {"id": 70, "type": json.loads(request.content)["variables"]["type"]},
        }
        return httpx2.Response(
            200,
            json={
                "data": {
                    "MediaListCollection": {
                        "user": {"mediaListOptions": {"scoreFormat": "POINT_5"}},
                        "lists": [{"entries": [item]}],
                    }
                }
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        context = ProviderContext(
            http=http,  # type: ignore[arg-type]
            config=AniListProvider.config_model(username="mini"),
            secrets={},
            log=logging.getLogger(__name__),
            state={},
        )
        records = [
            record
            async for record in AniListProvider().fetch(context, None, FetchMode.INCREMENTAL)
            if isinstance(record, RawRecord)
        ]

    opinion = AniListProvider().normalize(records[0]).opinions[0]
    assert opinion.rating_scale_id == "anilist-5"
    assert normalize_rating(opinion.rating_raw, _SCALES["POINT_5"]) == 75
    # The bug, stated as the number it produced: the same 4 read as 4 of 10.
    assert normalize_rating(opinion.rating_raw, _SCALES["POINT_10"]) == 40


def test_a_payload_retained_before_the_format_was_asked_for_keeps_its_old_scale() -> None:
    """Replay must not move a stored rating on a guess. One fetch supplies the real format."""
    stored_before = {
        "id": 9,
        "score": 8,
        "status": "COMPLETED",
        "updatedAt": 1700000000,
        "media": {"id": 90, "type": "ANIME", "title": {"romaji": "Old"}},
    }
    batch = AniListProvider().normalize(RawRecord(native_id="9", payload=stored_before))
    assert batch.opinions[0].rating_scale_id == "anilist-10"


def test_the_query_asks_for_the_score_format_it_normalizes_against() -> None:
    assert "scoreFormat" in _QUERY


def test_every_declared_scale_admits_its_format_s_own_values() -> None:
    """A scale that rejects a value the platform can emit turns a rating into an ingest failure."""
    for raw, score_format in ((100, "POINT_100"), (7.5, "POINT_10_DECIMAL"), (3, "POINT_3")):
        scale = _SCALES[score_format]
        assert scale.admits(Decimal(str(raw))), f"{score_format} rejects {raw}"
    assert normalize_rating(Decimal(1), _SCALES["POINT_3"]) == 0
    assert normalize_rating(Decimal(100), _SCALES["POINT_100"]) == 100
