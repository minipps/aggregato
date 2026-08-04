"""First-page and deep-page keyset latency measurements for a seeded log ."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select

from aggregato.db.schema import entries
from tests.bench.seed import seed_entries

SAMPLE_ENTRIES = 20_000


def _milliseconds(fn: object) -> float:
    started = time.perf_counter()
    fn()
    return (time.perf_counter() - started) * 1_000


def measure_entry_pages(database: Path, entries_count: int = SAMPLE_ENTRIES) -> dict[str, float]:
    """Seed and measure first/deep keyset pages, returning milliseconds for baseline recording."""
    seed_entries(database, entries_count)
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as conn:
            base = select(entries.c.id).where(
                entries.c.deleted_at.is_(None), entries.c.subject_ref.is_(None)
            )
            first_ms = _milliseconds(
                lambda: conn.execute(
                    base.order_by(entries.c.logged_at.desc(), entries.c.id.desc()).limit(51)
                ).all()
            )
            cursor_time = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=entries_count - 100)
            deep_ms = _milliseconds(
                lambda: conn.execute(
                    base.where(entries.c.logged_at < cursor_time)
                    .order_by(entries.c.logged_at.desc(), entries.c.id.desc())
                    .limit(51)
                ).all()
            )
    finally:
        engine.dispose()
    return {"entries_first_page_ms": first_ms, "entries_deep_page_ms": deep_ms}


def test_first_and_deep_keyset_pages_fit_baseline(tmp_path: Path) -> None:
    """Keyset page latency stays bounded when the cursor is near the end of a large archive."""
    result = measure_entry_pages(tmp_path / "entries.db")
    first_ms = result["entries_first_page_ms"]
    deep_ms = result["entries_deep_page_ms"]
    assert first_ms < 200
    assert deep_ms < 300
