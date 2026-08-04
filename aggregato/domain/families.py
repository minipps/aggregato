"""``media_type`` → ``media_family``.

A pure function over a total mapping, not a stored column (data-model.md §1): the grouping is a
property of the type, so storing it would mean two sources of truth and a migration every time one
drifted from the other. API ``media_family`` filters expand to a ``media_type IN (…)`` predicate,
which keeps individual types separately addressable .
"""

from __future__ import annotations

from collections.abc import Iterable

from .enums import MediaFamily, MediaType

_FAMILY_MEMBERS: dict[MediaFamily, frozenset[MediaType]] = {
    MediaFamily.SCREEN: frozenset(
        {
            MediaType.FILM,
            MediaType.TV,
            MediaType.ANIME,
        }
    ),
    MediaFamily.PRINT: frozenset({MediaType.BOOK, MediaType.COMIC, MediaType.MANGA}),
    MediaFamily.AUDIO: frozenset(
        {
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PODCAST,
            MediaType.PODCAST_EPISODE,
        }
    ),
    MediaFamily.INTERACTIVE: frozenset({MediaType.GAME}),
    MediaFamily.OTHER: frozenset({MediaType.OTHER}),
}

_FAMILY_OF: dict[MediaType, MediaFamily] = {
    media_type: family for family, members in _FAMILY_MEMBERS.items() for media_type in members
}


def family_of(media_type: MediaType) -> MediaFamily:
    """The family a media type belongs to.

    Total by construction — ``tests/unit/test_families.py`` asserts every type maps exactly once,
    so a newly added type that nobody assigned fails the suite rather than defaulting to ``other``.
    """
    return _FAMILY_OF[media_type]


def types_in_family(family: MediaFamily) -> frozenset[MediaType]:
    """The media types a ``media_family`` filter expands to ."""
    return _FAMILY_MEMBERS[family]


def types_in_families(families: Iterable[MediaFamily]) -> frozenset[MediaType]:
    """Expand several families at once, for the repeated ``media_family`` query parameter."""
    expanded: set[MediaType] = set()
    for family in families:
        expanded |= types_in_family(family)
    return frozenset(expanded)
