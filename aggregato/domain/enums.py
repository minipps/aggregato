"""Closed vocabularies used by providers and the storage contract.

Providers cannot add values. Persisted enum columns use text with ``CHECK`` constraints rather
than dialect-specific native enums; widening a stored vocabulary requires a migration.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint


class MediaType(StrEnum):
    FILM = "film"
    TV = "tv"
    BOOK = "book"
    COMIC = "comic"
    MANGA = "manga"
    ANIME = "anime"
    ALBUM = "album"
    TRACK = "track"
    GAME = "game"
    OTHER = "other"


class MediaFamily(StrEnum):
    SCREEN = "screen"
    PRINT = "print"
    AUDIO = "audio"
    INTERACTIVE = "interactive"
    OTHER = "other"


class EntryKind(StrEnum):
    WATCH = "watch"
    REWATCH = "rewatch"
    LISTEN = "listen"
    READ = "read"
    FINISH = "finish"
    PROGRESS = "progress"
    DROP = "drop"


COMPLETED_KINDS = frozenset(
    {EntryKind.WATCH, EntryKind.REWATCH, EntryKind.LISTEN, EntryKind.READ, EntryKind.FINISH}
)
"""The kinds a ``status=completed`` filter expands to — a finished consumption of the work.

Not a stored column, for the same reason ``media_family`` is not one (``domain/families.py``): the
grouping is a property of the kind, so storing it would mean two sources of truth. ``progress`` and
``drop`` are the deliberate exclusions — neither says the work was consumed to the end.
"""


class LoggedPrecision(StrEnum):
    """Precision the platform supplied for ``logged_at``.

    There is no default: one would silently claim more precision than the source supplied.
    """

    EXACT = "exact"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    """How an identity link was established. Manual links are preserved during resync."""

    ASSERTED = "asserted"
    MATCHED = "matched"
    MANUAL = "manual"


class CreatorKind(StrEnum):
    PERSON = "person"
    GROUP = "group"
    STUDIO = "studio"
    IMPRINT = "imprint"
    UNKNOWN = "unknown"


class AliasKind(StrEnum):
    PRIMARY = "primary"
    ALIAS = "alias"
    ROMANIZATION = "romanization"
    NATIVE = "native"
    CREDITED_AS = "credited_as"


class Role(StrEnum):
    AUTHOR = "author"
    ILLUSTRATOR = "illustrator"
    TRANSLATOR = "translator"
    EDITOR = "editor"
    DIRECTOR = "director"
    WRITER = "writer"
    COMPOSER = "composer"
    PERFORMER = "performer"
    FEATURED_PERFORMER = "featured_performer"
    VOICE = "voice"
    NARRATOR = "narrator"
    STUDIO = "studio"
    PUBLISHER = "publisher"
    DEVELOPER = "developer"
    OTHER = "other"


class ReviewFormat(StrEnum):
    PLAIN = "plain"
    MARKDOWN = "markdown"
    HTML = "html"


class ScaleKind(StrEnum):
    LINEAR = "linear"
    ORDINAL = "ordinal"


class Capability(StrEnum):
    POLL = "poll"
    BACKFILL = "backfill"
    FILE_IMPORT = "file_import"
    NOW_PLAYING = "now_playing"
    REPORTS_DELETES = "reports_deletes"
    HAS_RATINGS = "has_ratings"
    HAS_REVIEWS = "has_reviews"
    HAS_CREDITS = "has_credits"
    SCRAPES = "scrapes"
    PUSH = "push"
    """Reserved; ignored by provider API v1."""


class Acquisition(StrEnum):
    """Source category declared by a provider; selection follows ``CONTRIBUTING.md``."""

    API = "api"
    FEED = "feed"
    EXPORT = "export"
    SCRAPE = "scrape"


class ErrorClass(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TRANSPORT = "transport"
    PARSE = "parse"
    STRUCTURE_CHANGED = "structure_changed"
    BLOCKED = "blocked"
    INTERNAL = "internal"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class RunPhase(StrEnum):
    """The host-owned stage currently being performed by a sync run."""

    STARTING = "starting"
    CHECKING = "checking"
    REPLAYING = "replaying"
    FETCHING = "fetching"
    INGESTING = "ingesting"
    FINALIZING = "finalizing"
    FINISHED = "finished"
    FAILED = "failed"


class ProviderStatus(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    SYNCING = "syncing"
    DEGRADED = "degraded"
    MISCONFIGURED = "misconfigured"


class ResolutionSubject(StrEnum):
    WORK = "work"
    CREATOR = "creator"


class ResolutionDecision(StrEnum):
    LINKED = "linked"
    CREATED = "created"
    SPLIT = "split"
    IGNORED = "ignored"


class FetchMode(StrEnum):
    """What a run is asking the child to do. ``import`` sets ``ctx.import_path``."""

    INCREMENTAL = "incremental"
    FULL = "full"
    IMPORT = "import"
    REPLAY = "replay"
    CHECK = "check"


class IngestStage(StrEnum):
    """Where a record died, so an operator can tell a plugin bug from a host bug."""

    FETCH = "fetch"
    VALIDATE = "validate"
    NORMALIZE = "normalize"
    WRITE = "write"


def check_constraint(column: str, enum: type[StrEnum]) -> CheckConstraint:
    """Build the ``CHECK`` constraint that pins a text column to one vocabulary.

    Emitting these from the enum rather than writing the value lists into the schema by hand is
    what keeps a widened vocabulary from silently passing the database while failing the API.

    Args:
        column: Name of the text column to constrain. It is also the constraint's name; the
            table prefix comes from the metadata naming convention.
        enum: The vocabulary it must hold.

    Returns:
        A named ``CheckConstraint`` for use in a ``Table`` definition. Naming it explicitly is what
        lets Alembic drop and recreate it when a vocabulary widens — including under SQLite's
        batch mode, which rebuilds the table and needs to know what to put back.
    """
    values = ", ".join(f"'{member.value}'" for member in enum)
    # Named with the COLUMN only. schema.py's naming convention is
    # ck_%(table_name)s_%(constraint_name)s, so it supplies the prefix; spelling it here too
    # produced names like ck_creators_ck_creators_kind.
    return CheckConstraint(f"{column} IN ({values})", name=column)
