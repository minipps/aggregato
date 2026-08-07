"""Canonical provider identifiers and the closed namespace registry."""

from __future__ import annotations

import re

REGISTERED_NAMESPACES = frozenset(
    {
        "anilist",
        "anilist_person",
        "fixture",
        "goodreads",
        "goodreads_review",
        "goodreads_work",
        "imdb",
        "isbn",
        "isbn10",
        "isbn13",
        "koito",
        "koito_artist",
        "koito_track",
        "letterboxd",
        "listenbrainz",
        "mal",
        "mbid_album",
        "mbid_artist",
        "mbid_recording",
        "mbid_release",
        "mbid_release_group",
        "mbid_track",
        "mbid_work",
        "mbid_caa_release",
        "msid_recording",
        "msid_release",
        "musicbrainz_artist",
        "musicbrainz_recording",
        "musicbrainz_release",
        "olid",
        "openlibrary",
        "spotify",
        "tmdb",
        "tmdb_person",
        "tvdb",
        "wikidata",
    }
)

_ISBN10 = re.compile(r"^[0-9]{9}[0-9X]$")
_ISBN13 = re.compile(r"^[0-9]{13}$")


class IdentifierError(ValueError):
    """An identifier is not registered, non-empty, or checksum-valid."""


def canonicalize_identifier(namespace: str, value: str) -> tuple[str, str]:
    """Normalize and validate one asserted identifier before lookup or storage."""
    normalized_namespace = namespace.strip().casefold()
    normalized_value = value.strip()
    if normalized_namespace not in REGISTERED_NAMESPACES:
        raise IdentifierError(f"unregistered identifier namespace {namespace!r}")
    if not normalized_value:
        raise IdentifierError(f"identifier {normalized_namespace!r} has an empty value")
    if normalized_namespace == "isbn10":
        normalized_value = normalized_value.replace("-", "").replace(" ", "").upper()
        if not _ISBN10.fullmatch(normalized_value) or not _valid_isbn10(normalized_value):
            raise IdentifierError(f"invalid ISBN-10 value {value!r}")
    elif normalized_namespace == "isbn13":
        normalized_value = normalized_value.replace("-", "").replace(" ", "")
        if not _ISBN13.fullmatch(normalized_value) or not _valid_isbn13(normalized_value):
            raise IdentifierError(f"invalid ISBN-13 value {value!r}")
    elif normalized_namespace == "isbn":
        normalized_value = normalized_value.replace("-", "").replace(" ", "").upper()
        if len(normalized_value) == 10:
            valid = bool(_ISBN10.fullmatch(normalized_value)) and _valid_isbn10(normalized_value)
        elif len(normalized_value) == 13:
            valid = bool(_ISBN13.fullmatch(normalized_value)) and _valid_isbn13(normalized_value)
        else:
            valid = False
        if not valid:
            raise IdentifierError(f"invalid ISBN value {value!r}")
    return normalized_namespace, normalized_value


def _valid_isbn10(value: str) -> bool:
    total = sum(
        (10 - index) * (10 if digit == "X" else int(digit)) for index, digit in enumerate(value)
    )
    return total % 11 == 0


def _valid_isbn13(value: str) -> bool:
    total = sum((1 if index % 2 == 0 else 3) * int(digit) for index, digit in enumerate(value[:12]))
    return (10 - total % 10) % 10 == int(value[-1])
