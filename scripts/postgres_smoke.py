"""Exercise the supported PostgreSQL path against the CI service.

This is intentionally a process-level smoke check rather than a pytest test: the ordinary test
suite unconditionally blocks sockets, while this job deliberately connects to its declared local
PostgreSQL service. It covers the database behaviours that SQLite cannot represent: migration DDL,
an atomic claim race, transactional release/retry state, writer persistence, and tsvector search.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aggregato.db.engine import create_engine, transaction
from aggregato.db.migrate import upgrade_to_head
from aggregato.db.schema import provider_state, providers, sync_runs, works
from aggregato.db.search import SearchKind, matching_ref_ids
from aggregato.domain.enums import (
    EntryKind,
    ErrorClass,
    LoggedPrecision,
    MediaType,
    ProviderStatus,
    RunStatus,
)
from aggregato.domain.models import NormalizedBatch, NormalizedEntry, NormalizedWork, RawRecord
from aggregato.ingest.writer import WriteContext, write_batches
from aggregato.sync.scheduler import claim, release

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


async def smoke(url: str) -> None:
    """Migrate and exercise one isolated provider row, then close the engine."""
    await asyncio.to_thread(upgrade_to_head, url)
    engine = create_engine(url)
    try:
        async with transaction(engine) as conn:
            await conn.execute(
                providers.insert().values(
                    id="fixture",
                    enabled=True,
                    status=str(ProviderStatus.IDLE),
                    acquisition="export",
                    schema_version=1,
                    reviewed=True,
                    config={},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await conn.execute(
                provider_state.insert().values(
                    provider_id="fixture",
                    effective_interval_seconds=3600,
                    consecutive_failures=0,
                    retry_step=0,
                    next_run_at=NOW,
                    kv={},
                )
            )

        claims = await asyncio.gather(
            claim(engine, "fixture", now=NOW),
            claim(engine, "fixture", now=NOW),
        )
        assert sum(claims) == 1, f"claim race admitted {claims!r}"

        async with transaction(engine) as conn:
            result = await conn.execute(
                sync_runs.insert()
                .values(
                    provider_id="fixture",
                    lineage_id=uuid.uuid4(),
                    attempt=1,
                    mode="incremental",
                    status="running",
                    started_at=NOW,
                )
                .returning(sync_runs.c.id)
            )
            run_id = int(result.scalar_one())

        await release(
            engine,
            "fixture",
            status=ProviderStatus.DEGRADED,
            next_run_at=NOW + timedelta(minutes=1),
            retry_step=1,
            consecutive_failures=1,
            now=NOW,
            run_id=run_id,
            run_status=RunStatus.FAILED,
            items_seen=1,
            items_written=0,
            items_failed=1,
            error_class=ErrorClass.TRANSPORT,
            error_message="postgres smoke retry",
        )

        async with transaction(engine) as conn:
            await write_batches(
                conn,
                WriteContext(
                    provider_id="fixture",
                    sync_run_id=run_id,
                    schema_version=1,
                    now=NOW,
                ),
                [
                    (
                        RawRecord(native_id="postgres-event", payload={"title": "smoke"}),
                        NormalizedBatch(
                            work=NormalizedWork(media_type=MediaType.FILM, title="Postgres Smoke"),
                            entries=[
                                NormalizedEntry(
                                    kind=EntryKind.WATCH,
                                    logged_at=NOW,
                                    logged_precision=LoggedPrecision.EXACT,
                                    native_id="postgres-entry",
                                )
                            ],
                        ),
                    )
                ],
            )
            ref_ids = await matching_ref_ids(conn, SearchKind.WORK_TITLE, "postgres smoke")
            assert ref_ids, "PostgreSQL tsvector search did not find the written work"

        async with transaction(engine) as conn:
            provider = (
                await conn.execute(
                    select(providers.c.status, provider_state.c.next_run_at)
                    .select_from(providers.join(provider_state))
                    .where(providers.c.id == "fixture")
                )
            ).one()
            assert provider.status == str(ProviderStatus.DEGRADED)
            assert provider.next_run_at is not None
            assert await conn.scalar(select(works.c.title).where(works.c.title == "Postgres Smoke"))
    finally:
        await engine.dispose()


def main() -> int:
    url = os.environ.get("AGGREGATO_POSTGRES_URL")
    if not url:
        raise SystemExit("AGGREGATO_POSTGRES_URL is required")
    asyncio.run(smoke(url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
