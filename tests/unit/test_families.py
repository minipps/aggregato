"""Every media type maps to exactly one family .

The interesting assertion is totality: a media type added to the enum without being assigned a
family fails here rather than silently landing in ``other``, where it would quietly distort every
family-filtered query.
"""

from __future__ import annotations

import pytest

from aggregato.domain.enums import MediaFamily, MediaType
from aggregato.domain.families import family_of, types_in_families, types_in_family


def test_every_media_type_maps_exactly_once() -> None:
    """Covers both directions: zero families is a gap, two is an ambiguity."""
    seen: dict[MediaType, list[MediaFamily]] = {t: [] for t in MediaType}
    for family in MediaFamily:
        for media_type in types_in_family(family):
            seen[media_type].append(family)
    wrong = {t: fs for t, fs in seen.items() if len(fs) != 1}
    assert wrong == {}, f"media types not in exactly one family: {wrong}"


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        (MediaType.FILM, MediaFamily.SCREEN),
        (MediaType.ANIME, MediaFamily.SCREEN),
        (MediaType.MANGA, MediaFamily.PRINT),
        (MediaType.TRACK, MediaFamily.AUDIO),
        (MediaType.PODCAST_EPISODE, MediaFamily.AUDIO),
        (MediaType.GAME, MediaFamily.INTERACTIVE),
        (MediaType.OTHER, MediaFamily.OTHER),
    ],
)
def test_family_of(media_type: MediaType, expected: MediaFamily) -> None:
    assert family_of(media_type) is expected


def test_family_membership_round_trips() -> None:
    for media_type in MediaType:
        assert media_type in types_in_family(family_of(media_type))


def test_types_in_families_expands_and_unions() -> None:
    expanded = types_in_families([MediaFamily.PRINT, MediaFamily.INTERACTIVE])
    assert expanded == {MediaType.BOOK, MediaType.COMIC, MediaType.MANGA, MediaType.GAME}


def test_types_in_families_of_nothing_is_empty() -> None:
    # An absent media_family filter must not silently become "every type".
    assert types_in_families([]) == frozenset()
