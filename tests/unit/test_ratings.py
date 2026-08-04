"""Rating normalization, both kinds, boundaries, and the missing-mapping case ."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from aggregato.domain.enums import ScaleKind
from aggregato.domain.ratings import RatingOutOfScale, RatingScale, normalize_rating

TEN_IN_HALVES = RatingScale(
    id="test:0-10-half",
    kind=ScaleKind.LINEAR,
    min_value=Decimal("0"),
    max_value=Decimal("10"),
    step=Decimal("0.5"),
)

FIVE_STARS = RatingScale(
    id="test:1-5-star",
    kind=ScaleKind.LINEAR,
    min_value=Decimal("1"),
    max_value=Decimal("5"),
    step=Decimal("1"),
)

THREE_POINT = RatingScale(
    id="test:ordinal-3",
    kind=ScaleKind.ORDINAL,
    min_value=Decimal("1"),
    max_value=Decimal("3"),
    step=Decimal("1"),
    # Deliberately not evenly spaced — the point of an ordinal scale is that the platform's own
    # labels carry the meaning, not the arithmetic between them.
    labels={"1": 20, "2": 65, "3": 100},
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Decimal("0"), 0),
        (Decimal("0.5"), 5),
        (Decimal("5"), 50),
        (Decimal("7.5"), 75),
        (Decimal("10"), 100),
    ],
)
def test_linear_scale_starting_at_zero(raw: Decimal, expected: int) -> None:
    assert normalize_rating(raw, TEN_IN_HALVES) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Decimal("1"), 0),
        (Decimal("2"), 25),
        (Decimal("3"), 50),
        (Decimal("5"), 100),
    ],
)
def test_linear_scale_not_starting_at_zero(raw: Decimal, expected: int) -> None:
    # One star out of five is the bottom of the scale, not 20% — the minimum is 1, not 0.
    assert normalize_rating(raw, FIVE_STARS) == expected


def test_linear_boundaries_are_exactly_zero_and_one_hundred() -> None:
    assert normalize_rating(TEN_IN_HALVES.min_value, TEN_IN_HALVES) == 0
    assert normalize_rating(TEN_IN_HALVES.max_value, TEN_IN_HALVES) == 100


def test_linear_rounds_half_up_not_to_even() -> None:
    # An 8-step scale lands exactly on .5 twice. Decimal's default is banker's rounding, which
    # would send 12.5 down to 12 and 37.5 up to 38 — inconsistent in a way nobody debugs.
    eight_step = RatingScale(
        id="test:0-8",
        kind=ScaleKind.LINEAR,
        min_value=Decimal("0"),
        max_value=Decimal("8"),
        step=Decimal("1"),
    )
    assert normalize_rating(Decimal("1"), eight_step) == 13  # 12.5
    assert normalize_rating(Decimal("3"), eight_step) == 38  # 37.5


def test_linear_seven_point_scale() -> None:
    seven_point = RatingScale(
        id="test:1-7",
        kind=ScaleKind.LINEAR,
        min_value=Decimal("1"),
        max_value=Decimal("7"),
        step=Decimal("1"),
    )
    assert normalize_rating(Decimal("3"), seven_point) == 33  # 33.33
    assert normalize_rating(Decimal("5"), seven_point) == 67  # 66.67
    assert normalize_rating(Decimal("6"), seven_point) == 83  # 83.33


@pytest.mark.parametrize(("raw", "expected"), [("1", 20), ("2", 65), ("3", 100)])
def test_ordinal_uses_its_declared_map(raw: str, expected: int) -> None:
    assert normalize_rating(raw, THREE_POINT) == expected


def test_ordinal_does_not_interpolate() -> None:
    # A linear reading of a 3-point scale would put the middle at 50. It is 65 because that is
    # what the platform's labels mean.
    assert normalize_rating(Decimal("2"), THREE_POINT) == 65


def test_ordinal_lowest_label_may_be_zero() -> None:
    scale = RatingScale(
        id="test:ordinal-zero-floor",
        kind=ScaleKind.ORDINAL,
        min_value=Decimal("1"),
        max_value=Decimal("2"),
        step=Decimal("1"),
        labels={"1": 0, "2": 100},
    )
    assert normalize_rating(Decimal("1"), scale) == 0


def test_ordinal_scale_missing_a_mapping_is_rejected_at_declaration() -> None:
    # Caught when the provider declares the scale, not when a user's rating happens to hit the gap.
    with pytest.raises(ValidationError, match="missing a mapping for 3"):
        RatingScale(
            id="test:ordinal-gap",
            kind=ScaleKind.ORDINAL,
            min_value=Decimal("1"),
            max_value=Decimal("3"),
            step=Decimal("1"),
            labels={"1": 0, "2": 50},
        )


def test_ordinal_scale_without_labels_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must carry a labels map"):
        RatingScale(
            id="test:ordinal-nolabels",
            kind=ScaleKind.ORDINAL,
            min_value=Decimal("1"),
            max_value=Decimal("3"),
            step=Decimal("1"),
        )


def test_ordinal_labels_must_be_within_range() -> None:
    with pytest.raises(ValidationError, match=r"outside 0\.\.100"):
        RatingScale(
            id="test:ordinal-overflow",
            kind=ScaleKind.ORDINAL,
            min_value=Decimal("1"),
            max_value=Decimal("2"),
            step=Decimal("1"),
            labels={"1": 0, "2": 150},
        )


def test_decimal_string_forms_find_the_same_label() -> None:
    assert normalize_rating("2.0", THREE_POINT) == 65


@pytest.mark.parametrize("raw", [Decimal("-1"), Decimal("10.5"), Decimal("11")])
def test_out_of_bounds_is_rejected(raw: Decimal) -> None:
    with pytest.raises(RatingOutOfScale, match="not a permitted value"):
        normalize_rating(raw, TEN_IN_HALVES)


def test_between_steps_is_rejected() -> None:
    # 7.3 on a half-step scale means the provider mapped its own scale wrong.
    with pytest.raises(RatingOutOfScale, match="not a permitted value"):
        normalize_rating(Decimal("7.3"), TEN_IN_HALVES)


def test_non_numeric_is_rejected() -> None:
    with pytest.raises(RatingOutOfScale, match="not a number"):
        normalize_rating("four stars", FIVE_STARS)


def test_inverted_bounds_are_rejected() -> None:
    with pytest.raises(ValidationError, match="max_value must exceed min_value"):
        RatingScale(
            id="test:inverted",
            kind=ScaleKind.LINEAR,
            min_value=Decimal("10"),
            max_value=Decimal("1"),
            step=Decimal("1"),
        )


def test_non_positive_step_is_rejected() -> None:
    with pytest.raises(ValidationError, match="step must be positive"):
        RatingScale(
            id="test:zero-step",
            kind=ScaleKind.LINEAR,
            min_value=Decimal("0"),
            max_value=Decimal("10"),
            step=Decimal("0"),
        )
