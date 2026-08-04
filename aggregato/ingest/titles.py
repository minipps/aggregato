"""Conservative title keys for work resolution .

This is an aid to proposing exact candidates, never proof of identity: the resolver additionally
requires a matching media type and year, and refuses to auto-link an ambiguous key.
"""

from __future__ import annotations

import re
import unicodedata

_ARTICLES = frozenset({"a", "an", "the"})
_EDITION_SUFFIX = re.compile(
    r"\s*(?:\(|\[)?(?:deluxe|expanded|remaster(?:ed)?|collector'?s|anniversary)"
    r"(?:\s+(?:edition|version))?(?:\)|\])?\s*$",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Return a stable, deliberately lossy title-match key.

    Case, accents, punctuation, leading English articles, and common edition labels do not identify
    a distinct work. Everything else remains: removing sequel numbers or subtitles would turn a
    helper into an unsafe merge heuristic.
    """
    value = unicodedata.normalize("NFKD", title)
    value = "".join(char for char in value if not unicodedata.combining(char)).casefold().strip()
    value = _EDITION_SUFFIX.sub("", value)
    value = _PUNCTUATION.sub(" ", value)
    words = _SPACE.sub(" ", value).strip().split()
    if words and words[0] in _ARTICLES:
        words.pop(0)
    return " ".join(words)
