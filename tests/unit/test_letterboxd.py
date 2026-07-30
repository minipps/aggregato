"""Letterboxd RSS records preserve watched dates, review markup, and IDs."""

from __future__ import annotations

from pathlib import Path

import pytest

from aggregato.domain.models import RawRecord
from aggregato.providers.errors import StructureChangedError
from aggregato.providers.letterboxd import LetterboxdProvider, _items


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
