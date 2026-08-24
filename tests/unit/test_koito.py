"""Koito's request shape, its backwards walk, and the envelope it refuses to read as empty."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from aggregato.domain.enums import FetchMode, Role
from aggregato.domain.models import Checkpoint, Cursor, RawRecord
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import AuthError, ProviderError, RateLimited, StructureChangedError
from aggregato.providers.koito import (
    KoitoConfig,
    KoitoProvider,
    listens_url,
    now_playing_url,
)

_FIXTURES = Path("tests/fixtures/koito")


def _ctx(respond: object, **config: object) -> ProviderContext:
    return ProviderContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(respond)),  # type: ignore[arg-type]
        config=KoitoConfig(base_url="https://koito.fixture", api_key="valid", **config),  # type: ignore[arg-type]
        secrets={},
        log=logging.getLogger(__name__),
        state={},
    )


def test_base_url_is_trimmed_and_the_api_path_is_appended() -> None:
    config = KoitoConfig(base_url=" http://koito.lan:4110/ ", api_key="key")

    assert config.base_url == "http://koito.lan:4110"
    assert listens_url(config) == "http://koito.lan:4110/apis/web/v1/listens"
    assert now_playing_url(config) == "http://koito.lan:4110/apis/web/v1/now-playing"


def test_base_url_rejects_the_api_path() -> None:
    with pytest.raises(ValidationError, match="stop before the API path"):
        KoitoConfig(base_url="http://koito.lan:4110/apis/web/v1", api_key="key")


def test_empty_required_api_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        KoitoConfig(base_url="https://koito.lan", api_key="")


async def test_backwards_walk_always_sends_from_and_steps_to_below_the_oldest_listen() -> None:
    """`from` is mandatory even for an unbounded walk: Koito ignores `to` when `from` is zero, so a
    request without it answers with an empty page that reads exactly like the end of the history."""
    seen: list[dict[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        to = request.url.params.get("to")
        name = (
            "page-1.json"
            if to is None
            else "page-2.json"
            if to == "1784570282"
            else "page-3-empty.json"
        )
        return httpx.Response(200, json=json.loads((_FIXTURES / name).read_text()))

    items = [item async for item in KoitoProvider().fetch(_ctx(respond), None, FetchMode.FULL)]

    assert all(params["from"] == "1" for params in seen)
    assert [params.get("to") for params in seen] == [None, "1784570282", "1784451760"]
    native_ids = [item.native_id for item in items if isinstance(item, RawRecord)]
    assert native_ids == ["1784570712:412", "1784570283:91", "1784452440:55", "1784451761:412"]
    # The final checkpoint clears `to_ts`, which is what makes the next incremental run start at the
    # top instead of resuming mid-history.
    last = [item for item in items if isinstance(item, Checkpoint)][-1]
    assert last.cursor.state == {"newest_ts": 1784570712, "to_ts": None}


async def test_incremental_starts_one_second_past_the_newest_listen_already_seen() -> None:
    seen: list[dict[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=json.loads((_FIXTURES / "page-3-empty.json").read_text()))

    cursor = Cursor(state={"newest_ts": 1784570712, "to_ts": None})
    async for _ in KoitoProvider().fetch(_ctx(respond), cursor, FetchMode.INCREMENTAL):
        pass

    assert seen == [{"limit": "100", "from": "1784570713"}]


def test_normalize_files_track_and_artist_ids_and_credits_every_artist() -> None:
    listen = json.loads((_FIXTURES / "page-1.json").read_text())["items"][1]
    batch = KoitoProvider().normalize(RawRecord(native_id="1784570283:91", payload=listen))

    assert [credit.creator_name for credit in batch.credits] == ["Ayako", "Rui"]
    assert {credit.role for credit in batch.credits} == {Role.PERFORMER}
    assert [(i.namespace, i.value) for i in batch.external_ids] == [("koito_track", "91")]
    assert [(i.namespace, i.value) for i in batch.creator_external_ids] == [
        ("koito_artist", "12"),
        ("koito_artist", "13"),
    ]
    assert batch.entries[0].logged_at.timestamp() == 1784570283


async def test_a_renamed_envelope_is_a_structure_change_not_an_empty_history() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=json.loads((_FIXTURES / "structure-changed.json").read_text())
        )

    with pytest.raises(StructureChangedError, match="no `items` list"):
        async for _ in KoitoProvider().fetch(_ctx(respond), None, FetchMode.FULL):
            pass


async def test_a_server_ignoring_the_timeframe_filter_does_not_loop_forever() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json.loads((_FIXTURES / "page-1.json").read_text()))

    cursor = Cursor(state={"newest_ts": None, "to_ts": 1_000_000})
    with pytest.raises(StructureChangedError, match="not honouring the timeframe filter"):
        async for _ in KoitoProvider().fetch(_ctx(respond), cursor, FetchMode.FULL):
            pass


async def test_fetch_resolves_relative_artwork_paths_against_the_configured_server() -> None:
    """Koito states image paths from its own root, and `normalize` has no config to resolve one
    against — so the payload the host retains has to carry the absolute URL already."""

    def respond(request: httpx.Request) -> httpx.Response:
        name = "page-1.json" if request.url.params.get("to") is None else "page-3-empty.json"
        return httpx.Response(200, json=json.loads((_FIXTURES / name).read_text()))

    records = [
        record
        async for record in KoitoProvider().fetch(_ctx(respond), None, FetchMode.FULL)
        if isinstance(record, RawRecord)
    ]
    batch = KoitoProvider().normalize(records[0])

    assert batch.work.image_url == (
        "https://koito.fixture/image/3f6a1c4e-0000-4000-8000-000000000001/large.webp"
    )
    # Every size is rewritten, not just the preferred one: the retained payload keeps the shape the
    # server sent.
    assert records[0].payload["track"]["image"]["xs"] == (
        "https://koito.fixture/image/3f6a1c4e-0000-4000-8000-000000000001/xs.webp"
    )


async def test_a_base_url_with_a_subpath_keeps_the_subpath() -> None:
    """Koito behind a reverse proxy at /koito: urljoin would discard the prefix and 404."""

    def respond(request: httpx.Request) -> httpx.Response:
        name = "page-1.json" if request.url.params.get("to") is None else "page-3-empty.json"
        return httpx.Response(200, json=json.loads((_FIXTURES / name).read_text()))

    ctx = ProviderContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(respond)),  # type: ignore[arg-type]
        config=KoitoConfig(base_url="https://host.fixture/koito", api_key="valid"),
        secrets={},
        log=logging.getLogger(__name__),
        state={},
    )
    records = [
        record
        async for record in KoitoProvider().fetch(ctx, None, FetchMode.FULL)
        if isinstance(record, RawRecord)
    ]

    assert KoitoProvider().normalize(records[0]).work.image_url == (
        "https://host.fixture/koito/image/3f6a1c4e-0000-4000-8000-000000000001/large.webp"
    )


def test_a_track_with_no_artwork_states_no_image_url() -> None:
    """Koito sends an empty string per size rather than omitting `image`."""
    listen = json.loads((_FIXTURES / "page-1.json").read_text())["items"][1]

    assert (
        KoitoProvider().normalize(RawRecord(native_id="x", payload=listen)).work.image_url is None
    )


def test_a_relative_path_is_never_handed_to_the_image_cache() -> None:
    """A payload stored before `fetch` resolved these holds a relative path. Storing it would spend
    a fetch attempt and a failure row on a URL that cannot resolve."""
    listen = json.loads((_FIXTURES / "page-1.json").read_text())["items"][0]
    assert listen["track"]["image"]["large"].startswith("/")

    assert (
        KoitoProvider().normalize(RawRecord(native_id="x", payload=listen)).work.image_url is None
    )


async def test_now_playing_uses_the_authenticated_endpoint_and_existing_track_normalization() -> (
    None
):
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json=json.loads((_FIXTURES / "now-playing-active.json").read_text())
        )

    item = await KoitoProvider().now_playing(_ctx(respond))

    assert item is not None
    assert str(seen[0].url) == "https://koito.fixture/apis/web/v1/now-playing"
    assert seen[0].headers["Authorization"] == "Token valid"
    assert dict(seen[0].url.params) == {}
    assert item.work.title == "Weightless"
    assert item.work.image_url == (
        "https://koito.fixture/image/3f6a1c4e-0000-4000-8000-000000000001/large.webp"
    )
    assert [(credit.creator_name, credit.position) for credit in item.credits] == [
        ("Marconi Union", 0)
    ]
    assert [(external_id.namespace, external_id.value) for external_id in item.external_ids] == [
        ("koito_track", "412")
    ]
    assert [
        (external_id.namespace, external_id.value) for external_id in item.creator_external_ids
    ] == [("koito_artist", "88")]


async def test_now_playing_returns_none_for_an_idle_response() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=json.loads((_FIXTURES / "now-playing-idle.json").read_text())
        )

    assert await KoitoProvider().now_playing(_ctx(respond)) is None


async def test_now_playing_rejects_a_malformed_active_response() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=json.loads((_FIXTURES / "now-playing-structure-changed.json").read_text())
        )

    with pytest.raises(StructureChangedError, match=r"active now-playing response.*track"):
        await KoitoProvider().now_playing(_ctx(respond))


async def test_now_playing_requires_a_boolean_activity_flag() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"currently_playing": "true"})

    with pytest.raises(StructureChangedError, match="boolean `currently_playing`"):
        await KoitoProvider().now_playing(_ctx(respond))


async def test_now_playing_rejects_invalid_json_as_a_structure_change() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(StructureChangedError, match="returned invalid JSON"):
        await KoitoProvider().now_playing(_ctx(respond))


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthError), (429, RateLimited), (503, ProviderError)],
)
async def test_now_playing_classifies_http_failures(
    status: int, error: type[ProviderError]
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(error):
        await KoitoProvider().now_playing(_ctx(respond))
