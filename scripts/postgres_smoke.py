"""Exercise the supported PostgreSQL path against the CI service.

This is intentionally a process-level smoke check rather than a pytest test: the ordinary test
suite unconditionally blocks sockets, while this job deliberately connects to its declared local
PostgreSQL service. It covers the database behaviours that SQLite cannot represent: concurrent
migration DDL, previous-revision data preservation, an atomic claim race, transactional release/
retry state, no-id writer idempotency, savepoint-atomic invalid records, writer persistence, and
tsvector search.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aggregato.db import migrate as migration_module
from aggregato.db.engine import create_engine, transaction
from aggregato.db.migrate import upgrade_to_head
from aggregato.db.schema import (
    entries,
    external_ids,
    provider_items,
    provider_state,
    providers,
    sync_runs,
    works,
)
from aggregato.db.search import SearchKind, matching_ref_ids
from aggregato.domain.enums import (
    EntryKind,
    ErrorClass,
    LoggedPrecision,
    MediaType,
    ProviderStatus,
    RunStatus,
    ScaleKind,
)
from aggregato.domain.models import (
    NormalizedBatch,
    NormalizedEntry,
    NormalizedOpinion,
    NormalizedWork,
    RawRecord,
)
from aggregato.domain.ratings import RatingScale
from aggregato.ingest.writer import WriteContext, write_batches
from aggregato.sync.scheduler import claim, release

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
PREVIOUS_REVISION = "0013"


def _upgrade_to_revision(url: str, revision: str) -> None:
    """Apply one migration revision for the previous-revision portability fixture."""
    config = Config()
    config.set_main_option("script_location", str(migration_module._SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, revision)


def _upgrade_to_head(url: str) -> None:
    """Run a head upgrade in a worker process, isolating Alembic's process-global context."""
    upgrade_to_head(url)


async def _concurrent_head_upgrades(url: str) -> None:
    """Run two cold-start migrations in separate processes against one database."""
    loop = asyncio.get_running_loop()
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        await asyncio.gather(
            loop.run_in_executor(pool, _upgrade_to_head, url),
            loop.run_in_executor(pool, _upgrade_to_head, url),
        )


async def _create_database(url: str, database: str) -> None:
    """Create an isolated PostgreSQL database for a migration-from-previous-revision check."""
    admin_url = make_url(url).set(database="postgres")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            # The name is generated locally from hex, so quoting it is sufficient identifier
            # protection without interpolating any caller-controlled URL content.
            await conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    finally:
        await engine.dispose()


async def _drop_database(url: str, database: str) -> None:
    """Remove the isolated migration fixture database after the smoke check."""
    admin_url = make_url(url).set(database="postgres")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
    finally:
        await engine.dispose()


async def _check_previous_revision_migration(url: str) -> None:
    """Seed revision 0013 in a temporary database, upgrade to head, and verify rows survive."""
    database = f"aggregato_previous_{uuid.uuid4().hex}"
    # URL.__str__ hides passwords; retain credentials because Alembic and the application engine
    # both need the full URL to connect to the temporary database.
    previous_url = make_url(url).set(database=database).render_as_string(hide_password=False)
    await _create_database(url, database)
    try:
        await asyncio.to_thread(_upgrade_to_revision, previous_url, PREVIOUS_REVISION)
        engine = create_engine(previous_url)
        previous_provider_id = "previous-fixture"
        requested_lineage_id = uuid.uuid4()
        work_id = uuid.uuid4()
        try:
            async with transaction(engine) as conn:
                await conn.execute(
                    providers.insert().values(
                        id=previous_provider_id,
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
                        provider_id=previous_provider_id,
                        effective_interval_seconds=3600,
                        consecutive_failures=0,
                        retry_step=0,
                        next_run_at=NOW,
                        requested_lineage_id=requested_lineage_id,
                        kv={},
                    )
                )
                await conn.execute(
                    works.insert().values(
                        id=work_id,
                        media_type=str(MediaType.FILM),
                        title="Previous Revision",
                        sort_title="previous revision",
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                await conn.execute(
                    external_ids.insert().values(
                        work_id=work_id,
                        namespace="tmdb",
                        value="previous-revision-1",
                        source="fixture",
                        confidence="asserted",
                        created_at=NOW,
                    )
                )
        finally:
            await engine.dispose()

        await asyncio.to_thread(upgrade_to_head, previous_url)
        engine = create_engine(previous_url)
        try:
            async with transaction(engine) as conn:
                provider = (
                    await conn.execute(
                        select(
                            providers.c.status,
                            provider_state.c.next_run_at,
                            provider_state.c.requested_lineage_id,
                        )
                        .select_from(providers.join(provider_state))
                        .where(providers.c.id == previous_provider_id)
                    )
                ).one()
                assert provider.status == str(ProviderStatus.IDLE)
                assert provider.next_run_at == NOW
                assert provider.requested_lineage_id == requested_lineage_id
                row = (
                    await conn.execute(
                        select(works.c.title, external_ids.c.value)
                        .join(external_ids, external_ids.c.work_id == works.c.id)
                        .where(works.c.id == work_id)
                    )
                ).one()
                assert row.title == "Previous Revision"
                assert row.value == "previous-revision-1"
        finally:
            await engine.dispose()
    finally:
        await _drop_database(url, database)


async def smoke(url: str) -> None:
    """Migrate and exercise one isolated provider row, then close the engine."""
    await _check_previous_revision_migration(url)
    # API and worker can cold-start together. The migration environment owns a PostgreSQL advisory
    # lock, so this must succeed against a genuinely empty database rather than only proving that a
    # second upgrade is idempotent after the first one has finished.
    await _concurrent_head_upgrades(url)
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

        no_id_record = (
            RawRecord(native_id="postgres-no-id-item", payload={"title": "no-id"}),
            NormalizedBatch(
                work=NormalizedWork(media_type=MediaType.FILM, title="Postgres No ID"),
                entries=[
                    NormalizedEntry(
                        kind=EntryKind.WATCH,
                        logged_at=NOW,
                        logged_precision=LoggedPrecision.EXACT,
                        native_id=None,
                    )
                ],
            ),
        )
        writer_context = WriteContext(
            provider_id="fixture",
            sync_run_id=run_id,
            schema_version=1,
            now=NOW,
        )

        async def write_no_id_record() -> None:
            """Use separate connections so PostgreSQL's unique-key race is exercised."""
            async with transaction(engine) as conn:
                await write_batches(conn, writer_context, [no_id_record])

        await asyncio.gather(write_no_id_record(), write_no_id_record())
        async with transaction(engine) as conn:
            no_id_count = await conn.scalar(
                select(func.count())
                .select_from(entries)
                .where(entries.c.provider_id == "fixture", entries.c.native_id.is_(None))
            )
            assert no_id_count == 1, "concurrent PostgreSQL no-id writers duplicated the entry"

        invalid_record = (
            RawRecord(native_id="postgres-invalid-item", payload={"title": "invalid"}),
            NormalizedBatch(
                work=NormalizedWork(media_type=MediaType.FILM, title="Postgres Invalid"),
                entries=[
                    NormalizedEntry(
                        kind=EntryKind.WATCH,
                        logged_at=NOW,
                        logged_precision=LoggedPrecision.EXACT,
                        native_id="postgres-invalid-entry",
                    )
                ],
                opinions=[
                    NormalizedOpinion(
                        # The scale is declared, but 4.25 is between its half-star steps. This fails
                        # after the work/item/entry path has run and therefore exercises rollback.
                        rating_raw=Decimal("4.25"),
                        rating_scale_id="postgres-five-stars",
                    )
                ],
            ),
        )
        scale = RatingScale(
            id="postgres-five-stars",
            kind=ScaleKind.LINEAR,
            min_value=Decimal("0"),
            max_value=Decimal("5"),
            step=Decimal("0.5"),
        )
        async with transaction(engine) as conn:
            counts = await write_batches(
                conn,
                WriteContext(
                    provider_id="fixture",
                    sync_run_id=run_id,
                    schema_version=1,
                    now=NOW,
                    rating_scales={scale.id: scale},
                ),
                [invalid_record],
            )
            assert counts.written == 0 and counts.failed == 1
            assert (
                await conn.scalar(
                    select(func.count())
                    .select_from(provider_items)
                    .where(provider_items.c.native_id == "postgres-invalid-item")
                )
                == 0
            ), "invalid PostgreSQL record left a provider item behind"
            assert (
                await conn.scalar(
                    select(func.count())
                    .select_from(works)
                    .where(works.c.title == "Postgres Invalid")
                )
                == 0
            ), "invalid PostgreSQL record left a work behind"
            assert (
                await conn.scalar(
                    select(func.count())
                    .select_from(entries)
                    .where(entries.c.native_id == "postgres-invalid-entry")
                )
                == 0
            ), "invalid PostgreSQL record left an entry behind"

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
