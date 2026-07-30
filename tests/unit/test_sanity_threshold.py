"""The full-fetch count guard blocks destructive inference (T085, R21)."""

from __future__ import annotations

import pytest

from aggregato.sync.sanity import DEFAULT_MINIMUM_RATIO, assess_window


def test_first_full_window_establishes_a_baseline() -> None:
    result = assess_window(12, None)
    assert result.passed
    assert result.minimum_count is None


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [(100, 50, True), (100, 49, False), (3, 1, False), (3, 2, True)],
)
def test_default_threshold_is_half_of_the_previous_window(
    previous: int, current: int, expected: bool
) -> None:
    assert assess_window(current, previous).passed is expected


def test_threshold_is_configurable_and_rejects_invalid_ratios() -> None:
    assert assess_window(7, 10, minimum_ratio=0.7).passed
    with pytest.raises(ValueError):
        assess_window(10, 10, minimum_ratio=0)


def test_default_ratio_is_the_documented_fifty_percent() -> None:
    assert DEFAULT_MINIMUM_RATIO == 0.5
