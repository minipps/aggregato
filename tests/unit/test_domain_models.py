"""The wire contract's load-bearing guarantees .

Everything here protects a property the process boundary depends on: a lossless one-line JSON round
trip, and rejection of the four provider mistakes the contract calls out by name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aggregato.domain.enums import (
    Confidence,
    CreatorKind,
    EntryKind,
    ErrorClass,
    LoggedPrecision,
    MediaType,
    ReviewFormat,
    Role,
)
from aggregato.domain.models import (
    Checkpoint,
    CheckResult,
    Cursor,
    NormalizedBatch,
    NormalizedCreatorId,
    NormalizedCredit,
    NormalizedEntry,
    NormalizedExternalId,
    NormalizedOpinion,
    NormalizedWork,
    RawRecord,
)
from aggregato.domain.subject_ref import SubjectRef


def _batch() -> NormalizedBatch:
    return NormalizedBatch(
        work=NormalizedWork(
            media_type=MediaType.TV,
            title="Severance",
            original_title=None,
            release_year=2022,
            sequence_number=1,
            image_url="https://example.invalid/a.jpg",
            metadata={"tagline": "The work you do", "runtime": [47, 41]},
        ),
        entries=[
            NormalizedEntry(
                kind=EntryKind.WATCH,
                logged_at=datetime(2026, 3, 1, 21, 15, tzinfo=UTC),
                logged_precision=LoggedPrecision.EXACT,
                native_id="obs-1",
                subject_ref=SubjectRef(season=1, episode=4),
                progress_value=Decimal("0.75"),
                progress_unit="fraction",
            ),
            NormalizedEntry(
                kind=EntryKind.PROGRESS,
                logged_at=datetime(2026, 3, 2, tzinfo=UTC),
                logged_precision=LoggedPrecision.DAY,
                progress_value=Decimal("312"),
                progress_unit="pages",
            ),
        ],
        opinions=[
            NormalizedOpinion(
                rating_raw=Decimal("3.5"),
                rating_scale_id="letterboxd-5-star-halves",
                is_liked=True,
                review_text="Please enjoy each memory equally.",
                review_format=ReviewFormat.MARKDOWN,
                contains_spoilers=False,
                authored_at=datetime(2026, 3, 1, 21, 30, tzinfo=UTC),
            )
        ],
        credits=[
            NormalizedCredit(
                creator_name="Ben Stiller",
                creator_kind=CreatorKind.PERSON,
                role=Role.DIRECTOR,
                role_raw="Directed by",
                credited_as="Ben Stiller",
                position=0,
            )
        ],
        external_ids=[
            NormalizedExternalId(namespace="tmdb", value="95396", confidence=Confidence.ASSERTED)
        ],
        creator_external_ids=[
            NormalizedCreatorId(
                creator_name="Ben Stiller",
                namespace="tmdb_person",
                value="10859",
                confidence=Confidence.ASSERTED,
            )
        ],
    )


def test_batch_round_trips_as_one_json_line() -> None:
    line = _batch().model_dump_json()
    assert "\n" not in line  # the pipe framing is newline-delimited; a newline would split a record
    assert NormalizedBatch.model_validate_json(line) == _batch()


@pytest.mark.parametrize(
    "value",
    [
        RawRecord(native_id="42", payload={"nested": {"list": [1, None, "two"]}}),
        Checkpoint(cursor=Cursor(state={"page": 3, "after": "2026-03-01"})),
        Cursor(),
        CheckResult(ok=False, error_class=ErrorClass.AUTH, detail="401 from /me"),
    ],
)
def test_wire_types_round_trip(value: RawRecord | Checkpoint | Cursor | CheckResult) -> None:
    assert type(value).model_validate_json(value.model_dump_json()) == value


def test_logged_precision_is_required() -> None:
    # No default anywhere: a default would silently fabricate exactness .
    with pytest.raises(ValidationError, match="logged_precision"):
        NormalizedEntry(  # type: ignore[call-arg]
            kind=EntryKind.WATCH, logged_at=datetime(2026, 3, 1, tzinfo=UTC)
        )


def test_rating_raw_without_a_scale_is_rejected() -> None:
    with pytest.raises(ValidationError, match="rating_raw requires the rating_scale_id"):
        NormalizedOpinion(rating_raw=Decimal("4"))


def test_rating_scale_without_a_rating_is_fine() -> None:
    # The reverse is not an error: a provider may declare the scale it uses on a like-only opinion.
    assert NormalizedOpinion(rating_scale_id="imdb-10").rating_raw is None


def test_unknown_field_is_rejected() -> None:
    # Plugin-controlled producer: an unrecognised field is a provider bug, not a long tail.
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        NormalizedWork.model_validate(
            {"media_type": MediaType.FILM, "title": "Sorcerer", "runtime_minutes": 121}
        )


def test_open_dicts_stay_open() -> None:
    work = NormalizedWork.model_validate(
        {"media_type": MediaType.FILM, "title": "Sorcerer", "metadata": {"anything": [1, 2]}}
    )
    assert work.metadata == {"anything": [1, 2]}


def test_decimals_survive_the_round_trip_as_decimals() -> None:
    line = _batch().model_dump_json()
    # Serialized as a JSON string, not a float: 3.5 is exact but 0.1-style values are not, and a
    # rating that drifts by an epsilon fails its scale's step check for no explicable reason.
    assert '"3.5"' in line

    back = NormalizedBatch.model_validate_json(line)
    rating = back.opinions[0].rating_raw
    assert isinstance(rating, Decimal)
    assert rating == Decimal("3.5")
    assert back.entries[0].progress_value == Decimal("0.75")
    assert isinstance(back.entries[0].progress_value, Decimal)


def test_a_float_rating_never_becomes_the_stored_value() -> None:
    opinion = NormalizedOpinion(rating_raw=Decimal("0.1"), rating_scale_id="s")
    assert opinion.model_dump_json().find('"0.1"') != -1


def test_progress_value_requires_a_unit() -> None:
    with pytest.raises(ValidationError, match="42 of what"):
        NormalizedEntry(
            kind=EntryKind.WATCH,
            logged_at=datetime(2026, 3, 1, tzinfo=UTC),
            logged_precision=LoggedPrecision.DAY,
            progress_value=Decimal("42"),
        )


def test_naive_logged_at_is_rejected() -> None:
    # Every timestamp is UTC-aware (data-model.md preamble); a naive one is an unknown offset.
    with pytest.raises(ValidationError, match="should have timezone info"):
        NormalizedEntry(
            kind=EntryKind.WATCH,
            logged_at=datetime(2026, 3, 1),  # noqa: DTZ001
            logged_precision=LoggedPrecision.DAY,
        )


def test_role_raw_is_required_even_when_role_maps_cleanly() -> None:
    with pytest.raises(ValidationError, match="role_raw"):
        NormalizedCredit(  # type: ignore[call-arg]
            creator_name="Ben Stiller", role=Role.DIRECTOR, position=0
        )


def test_position_is_required_and_zero_based() -> None:
    with pytest.raises(ValidationError, match="position"):
        NormalizedCredit(  # type: ignore[call-arg]
            creator_name="X", role=Role.WRITER, role_raw="Story"
        )
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        NormalizedCredit(creator_name="X", role=Role.WRITER, role_raw="Story", position=-1)


def test_an_empty_batch_is_valid() -> None:
    # A provider that found a work but no activity for it is not an error.
    batch = NormalizedBatch(work=NormalizedWork(media_type=MediaType.GAME, title="Outer Wilds"))
    assert batch.entries == []
    assert batch.model_validate_json(batch.model_dump_json()) == batch
