"""Hardcover maps library state to the read events it attests to, and classifies API refusals."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aggregato.domain.enums import EntryKind, FetchMode, LoggedPrecision, Role
from aggregato.domain.models import NormalizedBatch, RawRecord
from aggregato.domain.ratings import normalize_rating
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import AuthError, StructureChangedError
from aggregato.providers.hardcover import _LIBRARY, SCALE, HardcoverConfig, HardcoverProvider

FIXTURES = Path("tests/fixtures/hardcover")


def _fixture_batches() -> dict[str, NormalizedBatch]:
    items = [
        item
        for name in ("page-1.json", "page-2.json")
        for item in json.loads((FIXTURES / name).read_text())["data"]["user_books"]
    ]
    return {
        str(item["id"]): HardcoverProvider().normalize(
            RawRecord(native_id=str(item["id"]), payload=item)
        )
        for item in items
    }


def test_each_status_logs_what_it_attests_to() -> None:
    batches = _fixture_batches()
    shape = {
        native_id: [(e.kind, e.logged_precision, e.native_id) for e in batch.entries]
        for native_id, batch in batches.items()
    }
    assert shape == {
        # Two finished reads, each at the precision the reader gave its date.
        "5001": [
            (EntryKind.READ, LoggedPrecision.DAY, "9001"),
            (EntryKind.READ, LoggedPrecision.MONTH, "9002"),
        ],
        "5002": [(EntryKind.PROGRESS, LoggedPrecision.EXACT, "9003")],
        "5003": [(EntryKind.DROP, LoggedPrecision.EXACT, "9004")],
        # Want to Read states nothing, and withdraws anything logged before.
        "5004": [],
        # Imported as Read with a date but no read-through: no platform id, deduplicated on date.
        "5005": [(EntryKind.READ, LoggedPrecision.DAY, None)],
    }
    assert [n for n, b in batches.items() if b.retracts_entries] == ["5004"]
    progress = batches["5002"].entries[0]
    assert (progress.progress_value, progress.progress_unit) == (Decimal(120), "pages")


def test_an_undated_finished_status_invents_no_read() -> None:
    item = {"id": 1, "status": 3, "book": {"id": 2, "title": "T"}, "user_book_reads": []}
    assert HardcoverProvider().normalize(RawRecord(native_id="1", payload=item)).entries == []


def test_identifiers_skip_a_checksum_invalid_isbn_but_keep_the_rest() -> None:
    batches = _fixture_batches()
    assert {(i.namespace, i.value) for i in batches["5001"].external_ids} == {
        ("hardcover", "101"),
        ("hardcover_edition", "301"),
        ("isbn13", "9780306406157"),
        ("isbn10", "0306406152"),
        ("asin", "B0FIXTURE1"),
    }
    # 9780306406158 fails its checksum; the host would reject the whole record over it.
    assert {(i.namespace, i.value) for i in batches["5003"].external_ids} == {
        ("hardcover", "103"),
        ("hardcover_edition", "303"),
    }


def test_credits_keep_the_platform_role_and_author_ids() -> None:
    batch = _fixture_batches()["5001"]
    assert [(c.creator_name, c.role, c.role_raw) for c in batch.credits] == [
        ("Fixture Author", Role.AUTHOR, "Author"),
        ("Fixture Translator", Role.TRANSLATOR, "Translator"),
    ]
    assert {(c.namespace, c.value) for c in batch.creator_external_ids} == {
        ("hardcover_author", "201"),
        ("hardcover_author", "202"),
    }


def test_half_star_rating_and_review() -> None:
    (opinion,) = _fixture_batches()["5001"].opinions
    assert opinion.rating_scale_id == SCALE.id
    assert opinion.rating_raw == Decimal("4.5")
    assert SCALE.admits(Decimal("4.5")) and normalize_rating(Decimal(5), SCALE) == 100
    assert opinion.review_text == "Recorded fixture review."
    assert opinion.contains_spoilers is False
    assert opinion.authored_at is not None and opinion.authored_at.tzinfo is not None


def test_the_query_asks_for_the_fields_the_mapping_reads() -> None:
    for field in ("status: status_id", "finished_at_precision", "last_read_date", "review_raw"):
        assert field in _LIBRARY


async def _fetch(respond: Any, token: str = "fixture-token") -> list[object]:  # noqa: S107
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        context = ProviderContext(
            http=http,  # type: ignore[arg-type]
            config=HardcoverConfig(token=token),
            secrets={},
            log=logging.getLogger(__name__),
            state={},
        )
        return [item async for item in HardcoverProvider().fetch(context, None, FetchMode.FULL)]


async def test_a_token_pasted_with_its_bearer_prefix_is_sent_once() -> None:
    seen: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["authorization"])
        return httpx2.Response(401, json={"error": "invalid_token"})

    with pytest.raises(AuthError):
        await _fetch(respond, token="Bearer abc123")
    assert seen == ["Bearer abc123"]


async def test_a_missing_scope_is_an_auth_error_that_names_it() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if "user_books" not in json.loads(request.content)["query"]:
            return httpx2.Response(200, json={"data": {"me": [{"id": 7}]}})
        return httpx2.Response(
            403,
            json={
                "error": "insufficient_scope",
                "error_description": "no",
                "scope": "read:library",
            },
        )

    with pytest.raises(AuthError, match="read:library"):
        await _fetch(respond)


async def test_graphql_validation_errors_are_a_structure_change() -> None:
    """Hasura answers a removed field with HTTP 200 and ``errors``; silence would erase nothing."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "errors": [
                    {
                        "message": "field 'rating' not found",
                        "extensions": {"code": "validation-failed"},
                    }
                ]
            },
        )

    with pytest.raises(StructureChangedError, match="rating"):
        await _fetch(respond)
