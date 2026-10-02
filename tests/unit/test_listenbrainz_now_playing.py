"""ListenBrainz current-playback requests and normalization parity."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from pydantic import SecretStr

from aggregato.domain.enums import Capability
from aggregato.domain.models import RawRecord
from aggregato.providers.base import NowPlayingProvider, ProviderContext
from aggregato.providers.errors import AuthError, RateLimited, StructureChangedError
from aggregato.providers.listenbrainz import (
    ListenBrainzConfig,
    ListenBrainzProvider,
    playing_now_url,
)

_FIXTURES = Path("tests/fixtures/listenbrainz")


def _fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((_FIXTURES / name).read_text()))


def _context(
    respond: Any,
    *,
    username: str = "listener-0001",
    token: str | None = None,
    base_url: str = "https://listenbrainz.fixture",
) -> ProviderContext:
    return ProviderContext(
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        config=ListenBrainzConfig(
            username=username, token=SecretStr(token or "valid"), base_url=base_url
        ),
        secrets={},
        log=logging.getLogger(__name__),
        state={},
    )


async def test_active_request_uses_quoted_user_endpoint_and_token_header() -> None:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=_fixture("now-playing-active.json"))

    context = _context(respond, username="listener name/1", base_url="https://host.fixture/prefix")
    try:
        item = await ListenBrainzProvider().now_playing(context)
    finally:
        await context.http.aclose()

    assert item is not None
    assert len(requests) == 1
    assert str(requests[0].url) == (
        "https://host.fixture/prefix/1/user/listener%20name%2F1/playing-now"
    )
    assert requests[0].headers["Authorization"] == "Token valid"
    assert dict(requests[0].url.params) == {}
    assert item.work.title == "First Fixture Track"
    assert item.work.metadata == {"release_name": "Fixture Release"}
    assert item.work.sequence_number == 1


async def test_idle_response_returns_no_item() -> None:
    context = _context(lambda request: httpx2.Response(200, json=_fixture("now-playing-idle.json")))
    try:
        assert await ListenBrainzProvider().now_playing(context) is None
    finally:
        await context.http.aclose()


async def test_current_item_matches_historical_normalization_without_an_entry() -> None:
    historical_listen = _fixture("page-1.json")["payload"]["listens"][0]
    historical = ListenBrainzProvider().normalize(
        RawRecord(
            native_id="1700000200:11111111-1111-4111-8111-111111111111", payload=historical_listen
        )
    )
    context = _context(
        lambda request: httpx2.Response(200, json=_fixture("now-playing-active.json"))
    )
    try:
        current = await ListenBrainzProvider().now_playing(context)
    finally:
        await context.http.aclose()

    assert current is not None
    assert current.work == historical.work
    assert current.credits == historical.credits
    assert current.external_ids == historical.external_ids
    assert current.creator_external_ids == historical.creator_external_ids
    assert historical.entries


async def test_multiple_current_listens_are_a_structure_change() -> None:
    context = _context(
        lambda request: httpx2.Response(200, json=_fixture("now-playing-multiple.json"))
    )
    try:
        with pytest.raises(StructureChangedError, match="multiple current listens"):
            await ListenBrainzProvider().now_playing(context)
    finally:
        await context.http.aclose()


async def test_malformed_playing_now_envelope_is_not_idle() -> None:
    context = _context(
        lambda request: httpx2.Response(200, json=_fixture("now-playing-structure-changed.json"))
    )
    try:
        with pytest.raises(StructureChangedError, match="boolean `playing_now`"):
            await ListenBrainzProvider().now_playing(context)
    finally:
        await context.http.aclose()


@pytest.mark.parametrize(
    ("status", "error"), [(401, AuthError), (429, RateLimited)], ids=["auth", "rate-limit"]
)
async def test_authentication_and_rate_limit_statuses_are_classified(
    status: int, error: type[Exception]
) -> None:
    body = _fixture(
        "credentials-invalid.json" if status == 401 else "now-playing-rate-limited.json"
    )
    context = _context(lambda request: httpx2.Response(status, json=body))
    try:
        with pytest.raises(error):
            await ListenBrainzProvider().now_playing(context)
    finally:
        await context.http.aclose()


def test_listenbrainz_declares_the_runtime_now_playing_capability() -> None:
    provider = ListenBrainzProvider()

    assert Capability.NOW_PLAYING in provider.capabilities
    assert isinstance(provider, NowPlayingProvider)
    assert (
        playing_now_url(
            ListenBrainzConfig(
                username="listener-0001", token=SecretStr("valid"), base_url="https://host"
            )
        )
        == "https://host/1/user/listener-0001/playing-now"
    )
