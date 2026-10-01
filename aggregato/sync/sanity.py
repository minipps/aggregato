"""Guard deletion inference against implausibly small full-fetch windows."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MINIMUM_RATIO = 0.5


@dataclass(frozen=True, slots=True)
class SanityResult:
    """Whether a window is plausible enough to permit the destructive inference path."""

    passed: bool
    previous_count: int | None
    current_count: int
    minimum_count: int | None


def assess_window(
    current_count: int, previous_count: int | None, *, minimum_ratio: float = DEFAULT_MINIMUM_RATIO
) -> SanityResult:
    """Compare like-for-like full-fetch counts without treating a first run as suspicious."""
    if not 0 < minimum_ratio <= 1:
        raise ValueError("minimum_ratio must be greater than 0 and no more than 1")
    if current_count < 0 or (previous_count is not None and previous_count < 0):
        raise ValueError("window item counts cannot be negative")
    if previous_count is None:
        return SanityResult(True, None, current_count, None)
    minimum = previous_count * minimum_ratio
    return SanityResult(current_count >= minimum, previous_count, current_count, int(minimum))
