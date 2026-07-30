"""The redacted Goodreads library-export shape stays faithfully interpreted."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from aggregato.domain.models import RawRecord
from aggregato.providers.errors import StructureChangedError
from aggregato.providers.goodreads import GoodreadsProvider, _rows

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
