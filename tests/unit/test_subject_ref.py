"""Every rejection branch of ``subject_ref`` validation .

Each case below is a way a provider can get this wrong, and each must be an ingest failure rather
than a silently dropped field — a dropped sub-unit key turns an episode record into a whole-series
record, which then contaminates every aggregate .
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aggregato.domain.subject_ref import SubjectRef, validate_subject_ref


def test_null_means_the_whole_work() -> None:
    assert validate_subject_ref(None) is None


def test_accepts_a_single_key() -> None:
    ref = validate_subject_ref({"episode": 4})
    assert ref is not None
    assert ref.as_dict() == {"episode": 4}


def test_accepts_several_keys() -> None:
    ref = validate_subject_ref({"season": 2, "episode": 4})
    assert ref is not None
    assert ref.as_dict() == {"season": 2, "episode": 4}


def test_as_dict_omits_unset_keys() -> None:
    # Storage form must be comparable: {"track": 7} and {"track": 7, "disc": None} are one thing.
    ref = SubjectRef(track=7)
    assert ref.as_dict() == {"track": 7}


def test_rejects_an_unknown_key() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        validate_subject_ref({"part": 3})


def test_rejects_an_unknown_key_alongside_a_valid_one() -> None:
    with pytest.raises(ValidationError):
        validate_subject_ref({"episode": 4, "segment": 1})


def test_rejects_zero() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        validate_subject_ref({"episode": 0})


def test_rejects_a_negative_value() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        validate_subject_ref({"season": -1})


def test_rejects_a_non_integer_value() -> None:
    with pytest.raises(ValidationError):
        validate_subject_ref({"episode": "pilot"})


def test_rejects_a_fractional_value() -> None:
    with pytest.raises(ValidationError):
        validate_subject_ref({"episode": 4.5})


def test_rejects_an_empty_object() -> None:
    # Not the same as null: an empty object means the provider tried to say something and failed.
    with pytest.raises(ValidationError, match="at least one"):
        validate_subject_ref({})


def test_rejects_explicit_nulls_only() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        validate_subject_ref({"season": None, "episode": None})


@pytest.mark.parametrize("value", [[], "s02e04", 4, True])
def test_rejects_a_non_object(value: object) -> None:
    with pytest.raises(ValueError, match="must be an object or null"):
        validate_subject_ref(value)


def test_is_frozen() -> None:
    # Ingest passes these around; a mutated ref after validation would defeat the boundary.
    ref = SubjectRef(episode=1)
    with pytest.raises(ValidationError):
        ref.episode = 2  # type: ignore[misc]
