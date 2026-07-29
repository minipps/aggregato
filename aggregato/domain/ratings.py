"""Rating normalization to 0–100 (FR-003, research.md R7).

Two kinds of scale, because platforms genuinely have two:

* **linear** — 0–10 in halves, 1–5 in whole stars. ``round(100 * (raw - min) / (max - min))``.
* **ordinal** — a fixed set of labels with no arithmetic between them ("liked" vs "loved", a
  three-point smiley scale). These carry an explicit ``value → normalized`` map, because
  interpolating between labels invents a precision the platform never had.

The normalized value is **derived and recomputable**: the raw value and the scale id are what get
stored, so a scale definition corrected later is repaired by replay rather than by re-syncing every
platform (the same principle as normalization replay, research.md R16).

A normalized value is comparable *within* a scale. It is not a cross-platform equivalence claim,
and the API says so rather than leaving the caller to assume otherwise.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, model_validator

from .enums import ScaleKind


class RatingScale(BaseModel):
    """A platform's rating scale, declared by the provider that uses it."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: ScaleKind
    min_value: Decimal
    max_value: Decimal
    step: Decimal
    labels: dict[str, int] | None = None
    """For ordinal scales: the raw value (as a string key) mapped to its 0–100 position."""

    @model_validator(mode="after")
    def _coherent(self) -> RatingScale:
        if self.max_value <= self.min_value:
            raise ValueError(f"scale {self.id}: max_value must exceed min_value")
        if self.step <= 0:
            raise ValueError(f"scale {self.id}: step must be positive")
        if self.kind is ScaleKind.ORDINAL:
            if not self.labels:
                raise ValueError(f"ordinal scale {self.id} must carry a labels map")
            missing = [str(v) for v in self.permitted_values() if str(v) not in self.labels]
            if missing:
                raise ValueError(
                    f"ordinal scale {self.id} is missing a mapping for {', '.join(missing)}; "
                    "an unmapped value would otherwise normalize to nothing at all"
                )
            out_of_range = {k: v for k, v in self.labels.items() if not 0 <= v <= 100}
            if out_of_range:
                raise ValueError(f"ordinal scale {self.id}: labels outside 0..100: {out_of_range}")
        return self

    def permitted_values(self) -> list[Decimal]:
        """Every value the scale admits, from ``min_value`` to ``max_value`` inclusive."""
        values: list[Decimal] = []
        value = self.min_value
        while value <= self.max_value:
            values.append(value)
            value += self.step
        return values

    def admits(self, raw: Decimal) -> bool:
        """Whether a raw value lies within bounds and lands on a step."""
        if not self.min_value <= raw <= self.max_value:
            return False
        return (raw - self.min_value) % self.step == 0


class RatingOutOfScale(ValueError):
    """A raw rating is outside its scale's bounds or lands between its steps."""


def normalize_rating(raw: Decimal | float | int | str, scale: RatingScale) -> int:
    """Convert a platform's raw rating to a 0–100 position within its own scale.

    Args:
        raw: The value the platform reported, verbatim.
        scale: The scale the provider declared for it.

    Returns:
        An integer 0–100.

    Raises:
        RatingOutOfScale: The value is not a number, is out of bounds, lands between steps, or —
            for an ordinal scale — has no mapping. Every one of these is a provider bug worth
            surfacing as an ingest failure rather than absorbing into a plausible-looking number.
    """
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise RatingOutOfScale(f"rating {raw!r} is not a number") from exc

    if not scale.admits(value):
        raise RatingOutOfScale(
            f"rating {value} is not a permitted value of scale {scale.id} "
            f"({scale.min_value}..{scale.max_value} step {scale.step})"
        )

    if scale.kind is ScaleKind.ORDINAL:
        assert scale.labels is not None  # guaranteed by the model validator
        # Explicit None checks, not `or`: the lowest label legitimately maps to 0, and a falsy
        # test here would silently discard it and report "no mapping".
        mapped = scale.labels.get(str(value))
        if mapped is None:
            mapped = scale.labels.get(_plain(value))
        if mapped is None:
            raise RatingOutOfScale(f"ordinal scale {scale.id} has no mapping for {value}")
        return mapped

    span = scale.max_value - scale.min_value
    position = Decimal(100) * (value - scale.min_value) / span
    # ROUND_HALF_UP rather than Decimal's banker's-rounding default: a 7-point scale's midpoint
    # rounding down on some values and up on others is the kind of thing nobody ever debugs.
    return int(position.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _plain(value: Decimal) -> str:
    """``Decimal('4.0')`` and ``Decimal('4')`` must find the same label key."""
    return str(value.normalize())
