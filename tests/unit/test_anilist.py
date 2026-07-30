"""AniList normalization preserves discrete scores and both staff and studio identities."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx

from aggregato.domain.enums import CreatorKind, EntryKind, FetchMode, Role, ScaleKind
from aggregato.domain.models import Cursor, NormalizedEntry, RawRecord
from aggregato.providers.anilist import _QUERY, AniListConfig, AniListProvider, _headers
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


async def test_a_stored_cursor_does_not_silence_later_runs() -> None:
    """The reported symptom: resyncing changed nothing because nothing was fetched.

    This provider used to checkpoint ``{"complete": True}`` and return early on it, so every run
    after the first yielded no records at all — no status change and no new title could ever arrive.
    A mutable list snapshot has to be re-read.
    """
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        media_type = json.loads(request.content)["variables"]["type"]
        item = {"id": media_type, "status": "COMPLETED", "media": {"type": media_type}}
        return httpx.Response(
            200, json={"data": {"MediaListCollection": {"lists": [{"entries": [item]}]}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
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

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"MediaListCollection": {"lists": []}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
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
