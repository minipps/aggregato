"""Phase 4 performance-budget declarations (T064, T068, T069, T082).

Measured benchmark runs are intentionally opt-in: CI asserts the versioned ceilings are present;
the operator benchmark harness consumes the same JSON on the target hardware.
"""

from __future__ import annotations

import json
from pathlib import Path


def test_phase_four_baseline_declares_required_budgets() -> None:
    baseline = json.loads((Path(__file__).parent / "baseline.json").read_text())
    budgets = baseline["budgets"]
    assert budgets["creator_resolution_per_minute"] >= 20_000
    assert budgets["entries_first_page_ms"] > 0
    assert budgets["entries_deep_page_ms"] > 0


def test_seed_harness_defaults_to_one_million_entries() -> None:
    """The operator-facing benchmark has the required archive size without a hidden flag."""
    from tests.bench.seed import seed_entries

    assert seed_entries.__defaults__ == (1_000_000,)
