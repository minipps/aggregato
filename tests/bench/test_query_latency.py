"""Single-run SQLite keyset projection timings for a small seeded log, not an API p95 budget."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select

from aggregato.api.pagination import Cursor, keyset_where
from aggregato.db.schema import entries
from tests.bench.seed import seed_entries

pytestmark = pytest.mark.bench

SAMPLE_ENTRIES = 20_000


def _milliseconds(fn: Callable[[], Any]) -> tuple[float, Any]:
    started = time.perf_counter()
    result = fn()
    return (time.perf_counter() - started) * 1_000, result


def measure_entry_pages(
    database: Path, entries_count: int = SAMPLE_ENTRIES
) -> dict[str, float | int]:
    """Measure one first and one >99%-deep projection page on the local SQLite database.

    Results characterize only this seed, projection, and one timing sample. They are not p95 or
    full-API measurements.
    """
    seed_entries(database, entries_count)
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as conn:
            base = select(entries.c.id).where(
                entries.c.deleted_at.is_(None), entries.c.subject_ref.is_(None)
            )
            first_ms, _ = _milliseconds(
                lambda: conn.execute(
                    base.order_by(entries.c.logged_at.desc(), entries.c.id.desc()).limit(51)
                ).all()
            )
            remaining = min(51, max(entries_count - 1, 0))
            cursor_time = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=remaining)
            cursor_id = conn.scalar(select(entries.c.id).where(entries.c.logged_at == cursor_time))
            assert cursor_id is not None
            cursor = Cursor(sort="logged_at", value=cursor_time, id=str(cursor_id))
            deep_ms, deep_rows = _milliseconds(
                lambda: conn.execute(
                    base.where(keyset_where(entries.c.logged_at, entries.c.id, "desc", cursor))
                    .order_by(entries.c.logged_at.desc(), entries.c.id.desc())
                    .limit(51)
                ).all()
            )
    finally:
        engine.dispose()
    return {
        "entries_first_page_ms": first_ms,
        "entries_deep_page_ms": deep_ms,
        "sample_entries": entries_count,
        "deep_page_rows": len(deep_rows),
        "entries_before_deep_cursor": entries_count - remaining - 1,
    }


def test_deep_page_measurement_uses_a_far_end_keyset_cursor(tmp_path: Path) -> None:
    """The measured page starts after at least 99% of this sample, with no timing threshold."""
    result = measure_entry_pages(tmp_path / "entries.db")
    assert result["deep_page_rows"] == 51
    assert result["entries_before_deep_cursor"] / result["sample_entries"] > 0.99
