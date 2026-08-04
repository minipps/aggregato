"""Letterboxd RSS records preserve watched dates, review markup, and IDs."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from aggregato.domain.enums import FetchMode
from aggregato.domain.models import Checkpoint, Cursor, RawRecord
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import StructureChangedError
from aggregato.providers.letterboxd import LetterboxdConfig, LetterboxdProvider, _items

_ACTIVITY_RSS = Path("tests/fixtures/letterboxd/activity.rss").read_bytes()


def _ctx(respond: object) -> ProviderContext:
    return ProviderContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(respond)),  # type: ignore[arg-type]
        config=LetterboxdConfig(username="example-user"),
        secrets={},
        log=logging.getLogger(__name__),
        state={},
    )


def test_letterboxd_rss_extracts_activity_and_identifiers() -> None:
    item = _items(Path("tests/fixtures/letterboxd/activity.rss").read_bytes())[0]
    batch = LetterboxdProvider().normalize(RawRecord(native_id=item["guid"], payload=item))
    assert batch.entries[0].kind == "watch"
    assert batch.entries[0].logged_precision == "day"
    assert {(identifier.namespace, identifier.value) for identifier in batch.external_ids} == {
        ("letterboxd", "letterboxd-review-100"),
        ("tmdb", "12345"),
    }
    assert batch.opinions[0].review_format == "html"


def test_changed_letterboxd_item_is_rejected() -> None:
    with pytest.raises(StructureChangedError):
        _items(Path("tests/fixtures/letterboxd/structure-changed.rss").read_bytes())


def test_imported_rss_derives_the_public_feed_username() -> None:
    payload = Path("tests/fixtures/letterboxd/activity.rss").read_bytes()

    assert LetterboxdProvider().config_from_import(payload) == {"username": "fixture_user"}


async def test_rss_is_refetched_on_every_automatic_poll() -> None:
    """A previous complete RSS snapshot must not disable subsequent scheduled refreshes."""
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=_ACTIVITY_RSS)

    provider = LetterboxdProvider()
    first = [item async for item in provider.fetch(_ctx(respond), None, FetchMode.INCREMENTAL)]
    second = [
        item
        async for item in provider.fetch(
            _ctx(respond),
            Cursor(state={"feed_complete": True}),
            FetchMode.INCREMENTAL,
        )
    ]

    assert requests == [
        "https://letterboxd.com/example-user/rss",
        "https://letterboxd.com/example-user/rss",
    ]
    assert next(item.cursor for item in first if isinstance(item, Checkpoint)).state == {}
    assert len([item for item in second if isinstance(item, RawRecord)]) == 2
