"""The provider wire contract: what a provider returns and how a fetch is resumed.

These models cross the process boundary between the plugin child and host parent, serialized as
**one JSON line** per message. Two consequences shape the model definitions:

* Every model must round-trip through ``model_dump_json()`` / ``model_validate_json()`` losslessly.
  Money-like values therefore use ``Decimal``, never ``float`` — a rating of ``3.5`` that arrives as
  ``3.4999999999999996`` fails its scale's step check for no reason a user could ever explain.
* Every model sets ``extra="forbid"``. The producer is plugin-controlled code, so an unrecognised
  field is a provider bug to surface at the boundary, not a long tail to absorb. The only
  exceptions are ``payload`` and ``metadata``, which are intentionally open dictionaries.

Field names match the ingest writer's insert fields, so it needs no translation table.
Host-assigned columns (surrogate ids, ``work_id``, ``provider_item_id``, ``source``,
``sort_title``, ``ingested_at``) are absent: a provider cannot know them, and accepting them would
invite a plugin to try.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .enums import (
    Confidence,
    CreatorKind,
    EntryKind,
    ErrorClass,
    LoggedPrecision,
    MediaType,
    ReviewFormat,
    Role,
)
from .subject_ref import SubjectRef

_STRICT = ConfigDict(extra="forbid")
FAILURE_ENVELOPE_VERSION = "aggregato.failure.v1"


class Cursor(BaseModel):
    """Opaque, provider-owned pagination state.

    The host persists it in ``provider_state.cursor`` and never interprets it; its shape is
    the provider's business, which is why the contents are an open dict while the envelope is not.
    """

    model_config = _STRICT

    state: dict[str, Any] = Field(default_factory=dict)


class Checkpoint(BaseModel):
    """A resume point yielded by ``fetch`` between records.

    The host stores the cursor, so a later run resumes here instead of restarting.
    It is a distinct type rather than a bare ``Cursor`` because ``fetch`` yields a union
    and the consumer must be able to tell a record from a resume point.
    """

    model_config = _STRICT

    cursor: Cursor


class RawRecord(BaseModel):
    """One record exactly as the platform served it, plus the id the platform calls it by.

    ``payload`` is retained verbatim in ``provider_items.raw_payload`` because it is the replay
    source: a corrected ``normalize`` is re-run over stored payloads rather than re-fetched.
    """

    model_config = _STRICT

    native_id: str
    payload: dict[str, Any]


class FailureEnvelope(BaseModel):
    """The captured provider identity kept beside a rejected record's raw payload."""

    model_config = _STRICT

    envelope: Literal["aggregato.failure.v1"] = "aggregato.failure.v1"
    native_id: str | None
    raw_payload: dict[str, Any]


class NormalizedWork(BaseModel):
    """The item a record concerns."""

    model_config = _STRICT

    media_type: MediaType
    title: str
    original_title: str | None = None
    release_year: int | None = None
    sequence_number: int | None = None
    """Season or track number, where the platform states one."""
    image_url: str | None = None
    """Platform-supplied artwork URL; the host does not search external catalogs."""
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedEntry(BaseModel):
    """A logged event: a watch, a read, a progress update."""

    model_config = _STRICT

    kind: EntryKind
    logged_at: AwareDatetime
    logged_precision: LoggedPrecision
    """Required, with no default, to avoid inventing precision.

    A platform that gives only a date says ``day``, and the host can then refuse to
    display a time it never had."""
    native_id: str | None = None
    """``None`` where the platform gives the event no id of its own; such entries are deduplicated
    on ``(provider_item_id, kind, logged_at, subject_ref)`` by the writer instead."""
    subject_ref: SubjectRef | None = None
    progress_value: Decimal | None = None
    progress_unit: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _progress_is_interpretable(self) -> NormalizedEntry:
        if self.progress_value is not None and not self.progress_unit:
            raise ValueError("progress_value requires progress_unit: 42 of what?")
        if self.kind is EntryKind.PROGRESS and self.progress_value is None:
            raise ValueError("a progress entry must carry a progress_value")
        return self


class NormalizedOpinion(BaseModel):
    """A rating, a like, a review, or any combination the platform reported together."""

    model_config = _STRICT

    rating_raw: Decimal | None = None
    rating_scale_id: str | None = None
    subject_ref: SubjectRef | None = None
    is_liked: bool | None = None
    review_text: str | None = None
    review_format: ReviewFormat | None = None
    contains_spoilers: bool | None = None
    authored_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _raw_values_carry_their_interpretation(self) -> NormalizedOpinion:
        # A raw rating without its scale is uninterpretable and un-normalizable: 4 out of 5 and 4
        # out of 10 are not the same rating.
        if self.rating_raw is not None and not self.rating_scale_id:
            raise ValueError("rating_raw requires the rating_scale_id it was measured on")
        if self.review_text is not None and self.review_format is None:
            raise ValueError("review_text requires review_format: rendering it otherwise guesses")
        return self


class NormalizedCredit(BaseModel):
    """A creator's involvement in the work, as this platform states it."""

    model_config = _STRICT

    creator_name: str
    """As the platform writes it. Identity resolution is the host's job, not the provider's."""
    creator_kind: CreatorKind = CreatorKind.UNKNOWN
    role: Role
    role_raw: str
    """The platform's own word, verbatim, even when ``role`` maps cleanly.

    "Screenplay" and "Story" both map to ``writer``, and the distinction survives only
    here."""
    credited_as: str | None = None
    position: int = Field(ge=0)
    """0 = first-billed. Where the platform does not express billing order, this is payload order
     — which is information, not noise, and is why it has no default."""


class NormalizedExternalId(BaseModel):
    """An identifier for the work found in the payload.

    Every identifier present is extracted, including ones Aggregato has no use for: under the
    no-enrichment rule this is the single largest lever on match quality.
    """

    model_config = _STRICT

    namespace: str
    value: str
    confidence: Confidence


class NormalizedCreatorId(BaseModel):
    """An identifier for a creator found in the payload.

    ``creator_name`` is the join key back to the credits in the same batch, because a provider has
    no host-side creator id to refer to.

    Known ceiling: two distinct creators sharing a name within one payload cannot be told apart
    here, and their identifiers would both attach to whichever the writer resolves first. Rare
    enough to accept for now; the upgrade path is to key on the credit's ``position`` instead, which
    is already unique within a batch. Revisit when a real payload hits it rather than before.
    """

    model_config = _STRICT

    creator_name: str
    namespace: str
    value: str
    confidence: Confidence


class NormalizedBatch(BaseModel):
    """Everything one raw record yielded. The unit ``normalize`` returns."""

    model_config = _STRICT

    work: NormalizedWork
    entries: list[NormalizedEntry] = Field(default_factory=list)
    opinions: list[NormalizedOpinion] = Field(default_factory=list)
    credits: list[NormalizedCredit] = Field(default_factory=list)
    external_ids: list[NormalizedExternalId] = Field(default_factory=list)
    creator_external_ids: list[NormalizedCreatorId] = Field(default_factory=list)
    retracts_entries: bool = False
    """The platform has **withdrawn** what this record previously logged, so the host should
    tombstone it (writer ``_retract_entries``).

    Only a provider can know this, which is why it is stated rather than inferred. An empty
    ``entries`` list is ambiguous on its own: for AniList a title moved back to ``PLANNING`` means
    the account withdrew the watch, while for Goodreads a row with no Date Read merely means the
    export does not say when it was read. Tombstoning the second on the strength of the first
    destroys real history — so the default is ``False`` and silence never deletes.

    Set it only where the platform's record is mutable *state* rather than an immutable *event*.
    Requires ``entries`` to be empty: a record cannot log an event and withdraw it at once."""

    @model_validator(mode="after")
    def _retraction_states_nothing_else(self) -> NormalizedBatch:
        if self.retracts_entries and self.entries:
            raise ValueError("retracts_entries cannot be combined with entries of its own")
        return self


class NowPlayingItem(BaseModel):
    """The current work a provider reports, without turning playback into history."""

    model_config = _STRICT

    work: NormalizedWork
    credits: list[NormalizedCredit] = Field(default_factory=list)
    external_ids: list[NormalizedExternalId] = Field(default_factory=list)
    creator_external_ids: list[NormalizedCreatorId] = Field(default_factory=list)


class CheckResult(BaseModel):
    """The outcome of a provider's credential and reachability self-test.

    Returned rather than raised, because ``check`` is a diagnostic the operator asked for: its
    failure is the answer, not an error.
    """

    model_config = _STRICT

    ok: bool
    error_class: ErrorClass | None = None
    detail: str | None = None
