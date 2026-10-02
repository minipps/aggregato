"""Measure API latency and writer throughput with local synthetic SQLite data.

Run from the repository root with ``.venv/bin/python scripts/benchmark_archive.py``. The script
leaves its databases in a fresh ``/tmp`` directory unless ``--workdir`` is supplied.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import func, insert, select, update

from aggregato.api.pagination import Cursor, encode_cursor
from aggregato.config import load_config
from aggregato.db.engine import create_engine
from aggregato.db.schema import entries, providers, sync_runs
from aggregato.db.search import sync_create_search_index
from aggregato.domain.enums import (
    Acquisition,
    EntryKind,
    LoggedPrecision,
    MediaType,
    ProviderStatus,
    RunPhase,
    RunStatus,
)
from aggregato.domain.models import (
    NormalizedBatch,
    NormalizedEntry,
    NormalizedWork,
    RawRecord,
)
from aggregato.ingest.writer import WriteContext, write_batches
from aggregato.main import create_app
from tests.bench.seed import seed_entries

API_ROWS = 1_000_000
WRITER_BASELINES = (10_000, 1_000_000)
TOKEN = "synthetic-publication-benchmark-token"  # noqa: S105 -- synthetic local auth only
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
LOGGED_AT = datetime(2026, 1, 1, tzinfo=UTC)
WRITER_PROVIDER = "benchmark-writer"


def _p95(values: list[float]) -> float:
    """Return nearest-rank p95, in milliseconds."""
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


async def _measure_api(database: Path, samples: int) -> dict[str, Any]:
    """Measure complete filtered route requests through in-process ASGI transport."""
    sync_engine = create_sync_engine(f"sqlite:///{database}")
    try:
        with sync_engine.connect() as conn:
            cursor_index = min(51, API_ROWS - 1)
            cursor_time = LOGGED_AT + timedelta(seconds=cursor_index)
            cursor_id = conn.scalar(select(entries.c.id).where(entries.c.logged_at == cursor_time))
    finally:
        sync_engine.dispose()
    assert cursor_id is not None
    deep_cursor = encode_cursor(Cursor("logged_at", cursor_time, str(cursor_id)))

    config = load_config(
        {
            "AGGREGATO_TOKEN": TOKEN,
            "AGGREGATO_DATA": str(database.parent),
            "AGGREGATO_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        }
    )
    app = create_app(config, run_migrations=False)
    first_path = "/api/v1/entries?provider=benchmark&media_type=film&limit=50"
    deep_path = f"{first_path}&cursor={deep_cursor}"
    timings: dict[str, list[float]] = {"first": [], "deep": []}

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://benchmark",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):

        async def request(label: str, path: str) -> None:
            started = time.perf_counter()
            response = await client.get(path)
            timings[label].append((time.perf_counter() - started) * 1_000)
            assert response.status_code == 200, response.text
            assert len(response.json()["items"]) == 50

        for _ in range(5):
            await request("first", first_path)
            await request("deep", deep_path)
        timings["first"].clear()
        timings["deep"].clear()
        for index in range(samples):
            if index % 2:
                await request("deep", deep_path)
                await request("first", first_path)
            else:
                await request("first", first_path)
                await request("deep", deep_path)

    first = timings["first"]
    deep = timings["deep"]
    return {
        "rows": API_ROWS,
        "filter": "provider=benchmark, media_type=film; default active work-level filters",
        "deep_cursor_after_fraction": round((API_ROWS - cursor_index - 1) / API_ROWS, 6),
        "samples_per_page": samples,
        "warmups_per_page": 5,
        "p95_method": "nearest rank",
        "first_ms": {
            "p50": round(statistics.median(first), 3),
            "p95": round(_p95(first), 3),
            "min": round(min(first), 3),
            "max": round(max(first), 3),
        },
        "deep_ms": {
            "p50": round(statistics.median(deep), 3),
            "p95": round(_p95(deep), 3),
            "min": round(min(deep), 3),
            "max": round(max(deep), 3),
        },
        "deep_p95_over_first_p95": round(_p95(deep) / _p95(first), 4),
    }


def _writer_worker(database: Path, count: int) -> dict[str, Any]:
    """Write one entry per synthetic provider record and capture process peak RSS."""

    async def write() -> dict[str, Any]:
        engine = create_engine(f"sqlite+aiosqlite:///{database}")
        async with engine.begin() as conn:
            before = await conn.scalar(select(func.count(entries.c.id)))
        if before is None:
            raise RuntimeError("could not count baseline entries")
        async with engine.begin() as conn:
            await conn.execute(
                insert(providers).values(
                    id=WRITER_PROVIDER,
                    enabled=True,
                    status=ProviderStatus.SYNCING,
                    acquisition=Acquisition.API,
                    schema_version=1,
                    reviewed=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            run_id = await conn.scalar(
                insert(sync_runs)
                .values(
                    provider_id=WRITER_PROVIDER,
                    lineage_id=uuid.uuid4(),
                    attempt=1,
                    mode="incremental",
                    status=RunStatus.RUNNING,
                    phase=RunPhase.INGESTING,
                    started_at=NOW,
                    updated_at=NOW,
                )
                .returning(sync_runs.c.id)
            )
        if run_id is None:
            raise RuntimeError("could not create synthetic sync run")

        work = NormalizedWork(media_type=MediaType.FILM, title="Benchmark work")
        records = [
            (
                RawRecord(native_id=f"writer-item-{index}", payload={"index": index}),
                NormalizedBatch(
                    work=work,
                    entries=[
                        NormalizedEntry(
                            kind=EntryKind.WATCH,
                            logged_at=LOGGED_AT + timedelta(seconds=API_ROWS + index),
                            logged_precision=LoggedPrecision.EXACT,
                            native_id=f"writer-entry-{index}",
                        )
                    ],
                ),
            )
            for index in range(count)
        ]
        ctx = WriteContext(
            provider_id=WRITER_PROVIDER,
            sync_run_id=int(run_id),
            schema_version=1,
            now=NOW,
        )
        started = time.perf_counter()
        async with engine.begin() as conn:
            result = await write_batches(conn, ctx, records)
            await conn.execute(
                update(sync_runs)
                .where(sync_runs.c.id == run_id)
                .values(
                    status=RunStatus.SUCCESS,
                    phase=RunPhase.FINISHED,
                    finished_at=NOW,
                    updated_at=NOW,
                )
            )
        seconds = time.perf_counter() - started
        await engine.dispose()
        if result.failed or result.written != count or result.entries_written != count:
            raise RuntimeError(f"writer returned unexpected counts: {result}")
        return {
            "baseline_entries": int(before),
            "records": count,
            "entries_written": result.entries_written,
            "writer_seconds": round(seconds, 3),
            "entries_per_hour": round(count / seconds * 3_600),
        }

    result = asyncio.run(write())
    # Linux reports ru_maxrss in KiB; macOS reports bytes.
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["peak_process_rss_mib"] = round(
        peak_rss / (1024**2 if sys.platform == "darwin" else 1024), 1
    )
    return result


def _snapshot(source: Path, destination: Path) -> None:
    """Copy a quiescent SQLite baseline with its own backup API."""
    import sqlite3

    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as target_conn:
        source_conn.backup(target_conn)


def _prepare_search_index(database: Path) -> None:
    """Create the dialect-specific search table that every production write maintains."""
    engine = create_sync_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as conn:
            sync_create_search_index(conn)
    finally:
        engine.dispose()


def _writer_trials(
    baseline: Path, workdir: Path, size: int, count: int, trials: int
) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for trial in range(trials):
        database = workdir / f"writer-{size}-trial-{trial + 1}.db"
        _snapshot(baseline, database)
        completed = subprocess.run(  # noqa: S603 -- own script and argument vector
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--writer-db",
                str(database),
                "--write-entries",
                str(count),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(completed.stdout)
        values.append(value)
        print(
            json.dumps(
                {
                    "writer_trial": trial + 1,
                    "baseline_entries": size,
                    **value,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
    rates = [int(value["entries_per_hour"]) for value in values]
    memories = [float(value["peak_process_rss_mib"]) for value in values]
    return {
        "baseline_entries": size,
        "entries_per_trial": count,
        "trials": values,
        "median_entries_per_hour": round(statistics.median(rates)),
        "peak_process_rss_mib": {"median": statistics.median(memories), "max": max(memories)},
    }


def _environment() -> dict[str, Any]:
    memory = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory = line.split()[1]
                break
    except OSError:
        pass
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "logical_cpus": os.cpu_count(),
        "host_ram_mib": round(int(memory) / 1024) if memory else None,
        "versions": {
            package: version(package)
            for package in ("SQLAlchemy", "aiosqlite", "fastapi", "httpx2", "pydantic")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, help="directory for disposable SQLite files")
    parser.add_argument("--api-samples", type=int, default=101)
    parser.add_argument("--writer-entries", type=int, default=1_000)
    parser.add_argument("--writer-trials", type=int, default=3)
    parser.add_argument("--writer-db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--write-entries", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.writer_db:
        if args.write_entries is None or args.write_entries < 1:
            parser.error("--writer-db requires a positive --write-entries")
        print(json.dumps(_writer_worker(args.writer_db, args.write_entries), sort_keys=True))
        return
    if args.api_samples < 20 or args.writer_entries < 1 or args.writer_trials < 1:
        parser.error("sample, entry, and trial counts must be positive (API samples >= 20)")

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="aggregato-publication-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)
    api_db = workdir / "api-1m.db"
    writer_small_db = workdir / "writer-base-10k.db"
    databases = [api_db, writer_small_db] + [
        workdir / f"writer-{size}-trial-{trial + 1}.db"
        for size in WRITER_BASELINES
        for trial in range(args.writer_trials)
    ]
    for database in databases:
        if database.exists():
            parser.error(f"refusing to overwrite existing benchmark database: {database}")
    api_seed = seed_entries(api_db, API_ROWS)

    _prepare_search_index(api_db)
    api_result = asyncio.run(_measure_api(api_db, args.api_samples))
    print(json.dumps({"api": api_result}), file=sys.stderr, flush=True)
    small_seed = seed_entries(writer_small_db, WRITER_BASELINES[0])
    _prepare_search_index(writer_small_db)
    small_writer = _writer_trials(
        writer_small_db, workdir, WRITER_BASELINES[0], args.writer_entries, args.writer_trials
    )
    large_writer = _writer_trials(
        api_db, workdir, WRITER_BASELINES[1], args.writer_entries, args.writer_trials
    )
    ratio = large_writer["median_entries_per_hour"] / small_writer["median_entries_per_hour"]
    print(
        json.dumps(
            {
                "workdir": str(workdir.resolve()),
                "environment": _environment(),
                "seed": {
                    "method": (
                        "tests.bench.seed.seed_entries; production Core schema; synthetic only"
                    ),
                    "distribution": (
                        "one work, one original provider item, exact timestamps spaced 1s, "
                        "live work-level watch entries"
                    ),
                    "one_million_seed_seconds": api_seed["seconds"],
                    "ten_thousand_seed_seconds": small_seed["seconds"],
                },
                "api": api_result,
                "writer": {
                    "workload": (
                        f"{args.writer_entries:,} unique records, one provider item and one event "
                        "entry per record, matched to the seeded work"
                    ),
                    "small_archive": small_writer,
                    "million_entry_archive": large_writer,
                    "million_vs_10k_throughput_ratio": round(ratio, 4),
                    "million_vs_10k_delta_percent": round((ratio - 1) * 100, 2),
                },
                "limits": [
                    ("short synthetic trials do not establish sustained deployment throughput"),
                    (
                        "the seed has one work and one original provider item; it does not model "
                        "archive identity or credit diversity"
                    ),
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
