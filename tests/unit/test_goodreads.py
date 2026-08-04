"""Goodreads library-export and RSS shapes stay faithfully interpreted."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import httpx
import pytest

from aggregato.domain.enums import FetchMode
from aggregato.domain.models import Checkpoint, Cursor, RawRecord
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import StructureChangedError
from aggregato.providers.goodreads import (
    GoodreadsConfig,
    GoodreadsProvider,
    _items,
    _rows,
    _rss_url,
)

EXPORT = Path("tests/fixtures/goodreads/library_export.csv")


def _records() -> list[RawRecord]:
    with EXPORT.open(encoding="utf-8-sig", newline="") as source:
        return [
            RawRecord(native_id=row["Book Id"], payload=dict(row)) for row in csv.DictReader(source)
        ]


def test_goodreads_extracts_book_and_isbn_identifiers() -> None:
    batch = GoodreadsProvider().normalize(_records()[0])
    assert {(item.namespace, item.value) for item in batch.external_ids} == {
        ("goodreads", "101"),
        ("isbn", "0123456789"),
        ("isbn13", "9780123456789"),
    }
    assert batch.entries[0].logged_precision == "day"
    assert batch.opinions[0].rating_raw == 4


def test_goodreads_does_not_turn_date_added_into_a_read_event() -> None:
    batch = GoodreadsProvider().normalize(_records()[1])
    assert batch.entries == []
    assert batch.work.metadata["exclusive_shelf"] == "to-read"


def test_changed_goodreads_headers_are_rejected() -> None:
    with pytest.raises(StructureChangedError):
        _rows(Path("tests/fixtures/goodreads/structure-changed.csv"))


RSS = b"""<?xml version="1.0"?><rss><channel><item>
<guid>https://www.goodreads.com/review/show/77?utm_source=rss</guid><title>Example Book</title>
<book_id>101</book_id><author_name>Example Author</author_name><isbn>9780123456789</isbn>
<user_rating>4</user_rating><user_read_at>Tue, 02 Jan 2024 00:00:00 +0000</user_read_at>
<user_date_added>Mon, 01 Jan 2024 00:00:00 +0000</user_date_added>
<user_shelves>read, fiction</user_shelves><user_review>Great book</user_review>
<book_published>2020</book_published><book><num_pages>123</num_pages></book>
</item></channel></rss>"""


def test_goodreads_rss_maps_items_and_identifiers() -> None:
    books = _items(RSS)
    batch = GoodreadsProvider().normalize(RawRecord(native_id="101", payload=books[0]))
    assert batch.entries[0].logged_at.isoformat() == "2024-01-02T00:00:00+00:00"
    assert {(item.namespace, item.value) for item in batch.external_ids} == {
        ("goodreads", "101"),
        ("isbn", "9780123456789"),
        ("goodreads_review", "77"),
    }
    assert batch.opinions[0].review_text == "Great book"


def test_goodreads_rss_rejects_changed_payload() -> None:
    with pytest.raises(StructureChangedError, match="no items"):
        _items(b"<main>Goodreads redesigned this page</main>")


def test_goodreads_profile_url_becomes_rss_url() -> None:
    profile_url = GoodreadsConfig(
        profile_url="https://www.goodreads.com/user/show/155188990-mini"
    ).profile_url
    assert profile_url is not None
    assert _rss_url(profile_url) == "https://www.goodreads.com/review/list_rss/155188990-mini"


async def test_goodreads_rss_is_refetched_after_a_csv_import() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=RSS)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    context = ProviderContext(
        http=client,
        config=GoodreadsConfig(
            profile_url="https://www.goodreads.com/user/show/155188990-mini",
        ),
        secrets={},
        log=logging.getLogger(__name__),
        state={},
    )
    try:
        items = [item async for item in GoodreadsProvider().fetch(context, None, FetchMode.FULL)]
        after_import = [
            item
            async for item in GoodreadsProvider().fetch(
                context, Cursor(state={"import_complete": True}), FetchMode.INCREMENTAL
            )
        ]
    finally:
        await client.aclose()

    assert [item.native_id for item in items if isinstance(item, RawRecord)] == ["101"]
    assert [item.cursor.state for item in items if isinstance(item, Checkpoint)] == [
        {},
    ]
    assert [str(request.url) for request in requests] == [
        "https://www.goodreads.com/review/list_rss/155188990-mini",
        "https://www.goodreads.com/review/list_rss/155188990-mini",
    ]
    assert [item.native_id for item in after_import if isinstance(item, RawRecord)] == ["101"]
