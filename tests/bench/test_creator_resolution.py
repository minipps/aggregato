"""Batched creator-resolution throughput measurement (T068)."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine

from aggregato.db.schema import creator_aliases, creators, metadata
from aggregato.domain.enums import AliasKind, CreatorKind, MediaFamily, Role
from aggregato.domain.models import NormalizedBatch, NormalizedCredit, NormalizedWork
from aggregato.ingest.resolve_creator import resolve_creators
from aggregato.ingest.titles import normalize_title
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 1, 1, tzinfo=UTC)
LOOKUPS = 20_000
NAMES_PER_BATCH = 100


def _batch() -> NormalizedBatch:
    return NormalizedBatch(
        work=NormalizedWork(media_type="film", title="Benchmark"),
        credits=[
            NormalizedCredit(
                creator_name=f"Creator {number}",
                creator_kind=CreatorKind.PERSON,
                role=Role.DIRECTOR,
                role_raw="Director",
                position=number,
            )
            for number in range(NAMES_PER_BATCH)
        ],
    )


def _throughput(creator_count: int) -> float:
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            for number in range(creator_count):
                creator_id = uuid.uuid4()
                name = f"Creator {number}"
                connection.execute(
                    creators.insert().values(
                        id=creator_id,
                        kind="person",
                        name=name,
                        sort_name=normalize_title(name),
                        metadata={},
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                connection.execute(
                    creator_aliases.insert().values(
                        creator_id=creator_id,
                        name=name,
                        normalized=normalize_title(name),
                        media_family="screen",
                        kind=str(AliasKind.PRIMARY),
                        source="benchmark",
                    )
                )
            adapted = SyncConnectionAdapter(connection)
            batch = _batch()
            started = time.perf_counter()
            for _ in range(LOOKUPS // NAMES_PER_BATCH):
                resolved = asyncio.run(
                    resolve_creators(
                        adapted, batch, MediaFamily.SCREEN, source="benchmark", now=NOW
                    )
                )
                assert len(resolved) == NAMES_PER_BATCH
            elapsed = time.perf_counter() - started
    finally:
        engine.dispose()
    return LOOKUPS / elapsed * 60


def test_creator_resolution_meets_budget_and_stays_flat() -> None:
    """At least 20k lookups/minute; a 10x creator table must not cause a linear slowdown."""
    small = _throughput(200)
    large = _throughput(2_000)
    assert small >= 20_000
    assert large >= 20_000
    assert large >= small * 0.5
