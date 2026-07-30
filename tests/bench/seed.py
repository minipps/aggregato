"""Build a deterministic SQLite archive for the Phase 4 performance measurements (T064)."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select

from aggregato.db.schema import entries, metadata, provider_items, works


def seed_entries(
    path: Path, count: int = 1_000_000, *, batch_size: int = 10_000
) -> dict[str, float | int]:
    """Create ``count`` live entries suitable for first/deep keyset query measurements.

    The function is intentionally stdlib-invocable (``python -m tests.bench.seed``), writes only
    the supplied path, and uses the production schema/index definitions.  One provider item is
    enough because the benchmark measures pagination cost rather than ingest fan-out.
    """
    engine = create_engine(f"sqlite:///{path}")
    started = time.perf_counter()
    try:
        with engine.begin() as conn:
            metadata.create_all(conn)
            now = datetime(2026, 1, 1, tzinfo=UTC)
            work_id = uuid.uuid4()
            conn.execute(
                works.insert().values(
                    id=work_id,
                    media_type="film",
                    title="Benchmark work",
                    sort_title="benchmark work",
                    metadata={},
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                provider_items.insert().values(
                    provider_id="benchmark",
                    native_id="benchmark-work",
                    work_id=work_id,
                    title_as_given="Benchmark work",
                    raw_payload={},
                    schema_version=1,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            provider_item_id = conn.scalar(
                select(provider_items.c.id).where(provider_items.c.native_id == "benchmark-work")
            )
            assert provider_item_id is not None
            for offset in range(0, count, batch_size):
                stop = min(offset + batch_size, count)
                conn.execute(
                    entries.insert(),
                    [
                        {
                            "work_id": work_id,
                            "provider_id": "benchmark",
                            "provider_item_id": provider_item_id,
                            "native_id": f"benchmark-{index}",
                            "kind": "watch",
                            "logged_at": now + timedelta(seconds=index),
                            "logged_precision": "exact",
                            "metadata": {},
                            "ingested_at": now + timedelta(seconds=index),
                        }
                        for index in range(offset, stop)
                    ],
                )
    finally:
        engine.dispose()
    return {"entries": count, "seconds": round(time.perf_counter() - started, 3)}


def main() -> None:
    """Seed a database, defaulting to the required one-million-entry archive."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="SQLite database to create")
    parser.add_argument("--entries", type=int, default=1_000_000)
    parser.add_argument("--batch-size", type=int, default=10_000)
    args = parser.parse_args()
    if args.entries < 1:
        parser.error("--entries must be positive")
    print(json.dumps(seed_entries(args.path, args.entries, batch_size=args.batch_size)))


if __name__ == "__main__":  # pragma: no cover - command-line entry point
    main()
