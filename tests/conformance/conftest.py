"""Registration and shared fixtures for the provider conformance suite (contract §5).

This suite is a **merge gate** (testing guidance and V) and the third-party compliance proof
. It is therefore parametrized over a registration list rather than written against any one
provider: adding a provider means adding a ``Registration`` below, not writing new tests.

A provider registers by naming its id, the recorded fixture ``fetch`` should read, the fixture that
must make ``check`` fail, and how to build its config from a fixture path. Everything else is
derived: the provider object comes from ``load_provider``, and the records come out of the
provider's own ``fetch`` rather than from a second reader of the wire format written here — a
conformance suite that parsed the fixtures itself would be asserting against its own parser.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

from aggregato.domain.enums import Acquisition, Capability, FetchMode
from aggregato.domain.models import Checkpoint, Cursor, RawRecord
from aggregato.providers.base import Provider, ProviderContext
from aggregato.providers.registry import load_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from httpx import AsyncClient


@dataclass(frozen=True)
class Registration:
    """One provider entered into the gate.

    Attributes:
        provider_id: The slug, which is also the package name and the fixture directory name.
        records: Fixture under ``tests/fixtures/<id>/`` holding the recorded records, spanning at
            least two pages so group 6 has a checkpoint to resume from.
        invalid: Fixture that stands in for an invalid credential set, so group 7 can assert
            ``check`` reports rather than raises. For a file-based provider this is an unreadable
            file; for an API provider it is a rejected-credentials response.
        config: Builds the ``config_model`` kwargs from the fixture path being exercised. Providers
            that do not read a path ignore the argument.
        secrets: What the host would resolve from the environment. Placeholders only — a real
            credential in this tree is a review failure (tests/fixtures/README.md).
        structure_changed: A fixture whose structure no longer matches the reader. Required for
            scraping providers (group 9), meaningless for others.
    """

    provider_id: str
    records: str
    invalid: str
    config: Callable[[Path], dict[str, Any]] = lambda _path: {}
    secrets: dict[str, str] = field(default_factory=dict)
    structure_changed: str | None = None


REGISTERED = [
    Registration(
        provider_id="fixture",
        records="log-two-pages.jsonl",
        invalid="unreadable-not-json.jsonl",
        config=lambda path: {"path": path},
    ),
    Registration(
        provider_id="anilist",
        records="records.json",
        invalid="invalid.json",
        config=lambda path: {
            "username": "fixture-user",
            "token": "invalid" if path.name == "invalid.json" else "valid",
            "api_url": "https://anilist.fixture/graphql",
        },
    ),
    Registration(
        provider_id="listenbrainz",
        records="page-1.json",
        invalid="credentials-invalid.json",
        config=lambda path: {
            "username": "listener-0001",
            "token": "invalid" if path.name == "credentials-invalid.json" else "valid",
            "base_url": "https://listenbrainz.fixture",
        },
    ),
    Registration(
        provider_id="spotify",
        records="page-1.json",
        invalid="credentials-invalid.json",
        config=lambda path: {
            "client_id": "fixture-client",
            "client_secret": "fixture-secret",
            "refresh_token": "invalid"
            if path.name == "credentials-invalid.json"
            else "fixture-refresh",
        },
    ),
    Registration(
        provider_id="koito",
        records="page-1.json",
        invalid="credentials-invalid.json",
        config=lambda path: {
            "base_url": "https://koito.fixture",
            "api_key": "invalid" if path.name == "credentials-invalid.json" else "valid",
        },
    ),
    Registration(
        provider_id="goodreads",
        records="library_export.csv",
        invalid="structure-changed.csv",
        config=lambda path: {
            "profile_url": None
            if path.name == "structure-changed.csv"
            else "https://www.goodreads.com/user/show/155188990-fixture"
        },
    ),
    Registration(
        provider_id="letterboxd",
        records="activity.rss",
        invalid="structure-changed.rss",
        config=lambda path: {
            "username": None if path.name == "structure-changed.rss" else "fixture_user"
        },
    ),
]
"""Every bundled provider. ``test_every_bundled_provider_is_registered`` fails if one is missing, so
shipping a provider without entering it into the gate is not possible (contract §5)."""


class _InertHTTP:
    """Stands in for ``ctx.http``.

    ``ProviderContext.http`` is typed ``AsyncClient`` and the type must not be weakened (
    depends on the host owning the client), so this is cast at the one construction site below.
    Handing over an object that raises on **any** attribute access is safe here because a
    conformance test that touches the network is a failure, not a slow test (contract §5): the
    autouse socket blocker in tests/conftest.py already makes it one, and this makes the same
    mistake fail at the call rather than at the connect.
    """

    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"a conformance test reached ctx.http.{name}: conformance runs offline with no "
            "credentials, so a provider needing HTTP must be exercised against a recorded fixture"
        )


@pytest.fixture(params=REGISTERED, ids=[r.provider_id for r in REGISTERED])
def registration(request: pytest.FixtureRequest) -> Registration:
    return cast("Registration", request.param)


@pytest.fixture
def provider(registration: Registration) -> Provider:
    """The provider object, loaded exactly as the host loads it — no test-only construction."""
    return load_provider(registration.provider_id)


@pytest.fixture
def provider_fixtures(registration: Registration, fixtures_dir: Path) -> Path:
    """``tests/fixtures/<id>/``, which the README makes part of the contract."""
    return fixtures_dir / registration.provider_id


@pytest.fixture
def records_path(registration: Registration, provider_fixtures: Path) -> Path:
    return provider_fixtures / registration.records


def build_ctx(registration: Registration, path: Path, **overrides: Any) -> ProviderContext:
    """A context holding only what contract §2 says the host hands over."""
    http: AsyncClient
    if registration.provider_id == "listenbrainz":
        http = httpx.AsyncClient(transport=httpx.MockTransport(_listenbrainz_fixture(path)))
    elif registration.provider_id == "spotify":
        http = httpx.AsyncClient(transport=httpx.MockTransport(_spotify_fixture(path)))
    elif registration.provider_id == "anilist":
        http = httpx.AsyncClient(transport=httpx.MockTransport(_anilist_fixture(path)))
    elif registration.provider_id == "koito":
        http = httpx.AsyncClient(transport=httpx.MockTransport(_koito_fixture(path)))
    elif registration.provider_id == "goodreads":
        http = httpx.AsyncClient(transport=httpx.MockTransport(_goodreads_fixture(path)))
    elif registration.provider_id == "letterboxd":
        http = httpx.AsyncClient(transport=httpx.MockTransport(_letterboxd_fixture(path)))
    else:
        http = cast("AsyncClient", _InertHTTP())
    return ProviderContext(
        http=http,
        config=load_provider(registration.provider_id).config_model(**registration.config(path)),
        secrets=dict(registration.secrets),
        log=logging.getLogger(f"conformance.{registration.provider_id}"),
        state={},
        **overrides,
    )


def _listenbrainz_fixture(path: Path) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the recorded ListenBrainz pages without exposing provider code to httpx."""
    directory = path.parent

    def respond(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == "Token invalid":
            return httpx.Response(
                401, json=json.loads((directory / "credentials-invalid.json").read_text())
            )
        if request.url.path.endswith("/playing-now"):
            if path.name == "now-playing-rate-limited.json":
                return httpx.Response(429, json=json.loads(path.read_text()))
            return httpx.Response(200, json=json.loads(path.read_text()))
        max_ts = request.url.params.get("max_ts")
        name = (
            "page-1.json"
            if max_ts is None
            else "page-2.json"
            if max_ts == "1700000200"
            else "page-3-empty.json"
        )
        return httpx.Response(200, json=json.loads((directory / name).read_text()))

    return respond


def _spotify_fixture(path: Path) -> Callable[[httpx.Request], httpx.Response]:
    """Serve recorded Spotify token and recently-played responses without network access."""
    directory = path.parent

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            if path.name == "credentials-invalid.json":
                return httpx.Response(400, json=json.loads(path.read_text()))
            return httpx.Response(
                200, json=json.loads((directory / "credentials-valid.json").read_text())
            )

        before = request.url.params.get("before")
        name = (
            "page-1.json"
            if before is None
            else "page-2.json"
            if before == "1740009600000"
            else "page-3-empty.json"
            if before == "1740006000000"
            else None
        )
        if name is None:
            raise AssertionError(f"unexpected Spotify before cursor: {before}")
        return httpx.Response(200, json=json.loads((directory / name).read_text()))

    return respond


def _koito_fixture(path: Path) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the recorded Koito pages, keyed on the backwards `to` boundary the provider sends.

    `to` is one second below the oldest listen on `page-1.json` (2026-07-20T17:58:03Z), so a walk
    that computed the boundary wrongly gets the terminal empty page and fails group 6 rather than
    silently passing on call order.
    """
    directory = path.parent

    def respond(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == "Token invalid":
            return httpx.Response(
                401, json=json.loads((directory / "credentials-invalid.json").read_text())
            )
        if request.url.path.endswith("/now-playing"):
            return httpx.Response(200, json=json.loads(path.read_text()))
        to = request.url.params.get("to")
        name = (
            "page-1.json"
            if to is None
            else "page-2.json"
            if to == "1784570282"
            else "page-3-empty.json"
        )
        return httpx.Response(200, json=json.loads((directory / name).read_text()))

    return respond


def _anilist_fixture(path: Path) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == "Bearer invalid":
            return httpx.Response(401, json=json.loads(path.read_text()))
        return httpx.Response(
            200, json=json.loads(path.parent.joinpath("records.json").read_text())
        )

    return respond


_GOODREADS_RSS = b"""<?xml version="1.0"?><rss><channel>
<item><guid>https://www.goodreads.com/review/show/77?utm_source=rss</guid>
<title>Example Book</title><book_id>101</book_id><author_name>Example Author</author_name>
<isbn>9780306406157</isbn><user_rating>4</user_rating>
<user_read_at>Tue, 02 Jan 2024 00:00:00 +0000</user_read_at>
<user_date_added>Mon, 01 Jan 2024 00:00:00 +0000</user_date_added>
<user_shelves>read, fiction</user_shelves><user_review>Great book</user_review>
<book_published>2020</book_published><book><num_pages>123</num_pages></book>
</item></channel></rss>"""
_GOODREADS_INVALID_RSS = (
    b'<?xml version="1.0"?><rss><channel><item><title>Changed</title></item></channel></rss>'
)


def _goodreads_fixture(path: Path) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the recorded Goodreads RSS response for both valid and changed structures."""

    payload = _GOODREADS_INVALID_RSS if path.name == "structure-changed.csv" else _GOODREADS_RSS

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    return respond


def _letterboxd_fixture(path: Path) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the recorded Letterboxd RSS response selected by the registration fixture."""

    payload = path.read_bytes()

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    return respond


@pytest.fixture
def ctx(registration: Registration, records_path: Path) -> ProviderContext:
    return build_ctx(registration, records_path)


async def fetch_all(
    provider: Provider, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
) -> list[RawRecord | Checkpoint]:
    """Drain ``fetch`` into a list, records and checkpoints interleaved as yielded."""
    return [item async for item in provider.fetch(ctx, cursor, mode)]


def only_records(items: list[RawRecord | Checkpoint]) -> list[RawRecord]:
    return [item for item in items if isinstance(item, RawRecord)]


@pytest.fixture
async def raw_records(provider: Provider, ctx: ProviderContext) -> list[RawRecord]:
    """The provider's recorded records, obtained through its own ``fetch``.

    Groups 3, 4 and 5 run over these, so what they assert about is exactly what a real run would
    have handed to ``normalize`` and stored for replay .
    """
    records = only_records(await fetch_all(provider, ctx, None, FetchMode.FULL))
    assert records, "a registered provider's records fixture must contain at least one record"
    return records


def scrapes(provider: Provider) -> bool:
    """Whether group 9 applies: either declaration is enough to be a scraper for the contract."""
    return Capability.SCRAPES in provider.capabilities or provider.acquisition is Acquisition.SCRAPE
