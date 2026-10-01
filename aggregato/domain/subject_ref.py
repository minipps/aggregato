"""Validate which sub-unit of a work a record refers to.

The closed vocabulary contains six positive-integer keys: season, episode, track, disc, chapter,
and volume. ``None`` means the record pertains to the whole work. Parent-side ingest validation
rejects invalid values into ``ingest_failures`` rather than silently dropping them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

SUBJECT_REF_KEYS = ("season", "episode", "track", "disc", "chapter", "volume")


class SubjectRef(BaseModel):
    """A sub-unit reference. Every field optional, at least one set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    season: int | None = Field(default=None, ge=1)
    episode: int | None = Field(default=None, ge=1)
    track: int | None = Field(default=None, ge=1)
    disc: int | None = Field(default=None, ge=1)
    chapter: int | None = Field(default=None, ge=1)
    volume: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _at_least_one_key(self) -> SubjectRef:
        if not self.model_dump(exclude_none=True):
            raise ValueError(
                "subject_ref must set at least one of "
                f"{', '.join(SUBJECT_REF_KEYS)}; use null for a whole-work record"
            )
        return self

    def as_dict(self) -> dict[str, int]:
        """The storage form: only the keys that are set, so equality is comparable."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


def validate_subject_ref(value: object) -> SubjectRef | None:
    """Validate a ``subject_ref`` arriving from provider code.

    Args:
        value: ``None``, or a mapping of sub-unit keys to positive integers.

    Returns:
        A ``SubjectRef``, or ``None`` for a whole-work record.

    Raises:
        ValueError: The value is not a mapping, carries an unknown key, holds a non-integer or
            non-positive value, or is an empty object. An empty object is rejected rather than
            treated as ``None`` because it means the provider tried to say something and failed.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"subject_ref must be an object or null, got {type(value).__name__}")
    return SubjectRef.model_validate(value)
