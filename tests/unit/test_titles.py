"""Title normalization is intentionally narrow; these examples pin its safe boundaries."""

import pytest

from aggregato.ingest.titles import normalize_title


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("The Café: A Story", "cafe a story"),
        ("An Album (Deluxe Edition)", "album"),
        ("A Film — Remastered", "film"),
        ("Spider-Man 2", "spider man 2"),
    ],
)
def test_normalize_title(source: str, expected: str) -> None:
    assert normalize_title(source) == expected


def test_normalize_title_does_not_remove_identity_bearing_subtitles() -> None:
    assert normalize_title("Dune: Part Two") != normalize_title("Dune")
