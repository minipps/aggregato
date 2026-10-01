"""Regression tests for rejected-record envelopes and schema replay selection."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.schema import (
    metadata,
    provider_items,
    sync_runs,
)
from aggregato.db.search import sync_create_search_index
from aggregato.domain.enums import IngestStage, MediaType
from aggregato.domain.models import NormalizedBatch, NormalizedWork, RawRecord
from aggregato.ingest import normalize_replay
from aggregato.ingest.failures import (
    capture_failure,
    replay_failure,
    unresolved_failures,
)
from aggregato.sync.protocol import FailureMessage, decode, encode
from tests.unit._sync_connection import SyncConnectionAdapter

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[SyncConnectionAdapter]:
    engine = create_engine(f"sqlite:///{tmp_path / 'ingest-integrity.db'}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        sync_create_search_index(connection)
        connection.execute(
            sync_runs.insert().values(
                id=1,
                provider_id="test",
                lineage_id=uuid.uuid4(),
                attempt=1,
                mode="incremental",
                status="running",
                started_at=NOW,
            )
        )
        yield SyncConnectionAdapter(connection)
    engine.dispose()


@pytest.mark.parametrize(
    ("native_id", "payload"),
    [
        ("nested-native", {"metadata": {"event_key": "payload-value"}}),
        ("renamed-native", {"event_key": "payload-value"}),
        ("", {"id": "wrong-payload-id"}),
        ("absent-from-payload", {"title": "no identity field"}),
    ],
)
async def test_failure_envelope_persists_the_wire_identity_verbatim(
    conn: SyncConnectionAdapter,
    native_id: str,
    payload: dict[str, Any],
) -> None:
    message = FailureMessage(native_id=native_id, payload=payload, error="bad record")
    decoded = decode(encode(message))
    assert isinstance(decoded, FailureMessage)

    await capture_failure(
        conn,
        provider_id="test",
        sync_run_id=1,
        stage=IngestStage.NORMALIZE,
        error=decoded.error,
        raw_payload=dict(decoded.payload),
        now=NOW,
        native_id=decoded.native_id,
    )

    [failure] = await unresolved_failures(conn)
    assert failure.native_id == native_id
    assert failure.raw_payload == payload


async def test_replay_uses_captured_identity_instead_of_payload_keys(
    conn: SyncConnectionAdapter,
) -> None:
    await capture_failure(
        conn,
        provider_id="test",
        sync_run_id=1,
        stage=IngestStage.NORMALIZE,
        error="bad record",
        native_id="captured-native-id",
        raw_payload={"id": "wrong-payload-id", "nested": {"id": "also-wrong"}},
        now=NOW,
    )
    [failure] = await unresolved_failures(conn)
    observed: list[str] = []

    def normalize(raw: RawRecord) -> NormalizedBatch:
        observed.append(raw.native_id)
        return NormalizedBatch(work=NormalizedWork(media_type=MediaType.FILM, title="Recovered"))

    async def write(raw: RawRecord, _batch: NormalizedBatch) -> None:
        observed.append(raw.native_id)

    assert await replay_failure(
        conn,
        failure=failure,
        normalize=normalize,
        write=write,
        now=NOW + timedelta(minutes=1),
    )
    assert observed == ["captured-native-id", "captured-native-id"]


async def test_unidentified_failure_is_not_replayed_with_a_synthetic_id(
    conn: SyncConnectionAdapter,
) -> None:
    await capture_failure(
        conn,
        provider_id="test",
        sync_run_id=1,
        stage=IngestStage.NORMALIZE,
        error="legacy failure",
        raw_payload={"id": "must-not-be-used"},
        now=NOW,
    )
    [failure] = await unresolved_failures(conn)

    def normalize(_raw: RawRecord) -> NormalizedBatch:
        raise AssertionError("an unidentified failure must not reach the provider normalizer")

    async def write(_raw: RawRecord, _batch: NormalizedBatch) -> None:
        raise AssertionError("an unidentified failure must not reach the writer")

    assert not await replay_failure(
        conn,
        failure=failure,
        normalize=normalize,
        write=write,
        now=NOW,
    )


def _patch_transaction(monkeypatch: pytest.MonkeyPatch, conn: SyncConnectionAdapter) -> AsyncEngine:
    @asynccontextmanager
    async def fake_transaction(_engine: object) -> Iterator[SyncConnectionAdapter]:
        yield conn

    monkeypatch.setattr(normalize_replay, "transaction", fake_transaction)
    return cast(AsyncEngine, object())


async def test_replay_selection_returns_each_stale_row_not_the_provider_maximum(
    conn: SyncConnectionAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for item_id, native_id, version in (
        (1, "stale", 1),
        (2, "current", 2),
        (3, "stale-again", 1),
    ):
        await conn.execute(
            provider_items.insert().values(
                id=item_id,
                provider_id="test",
                native_id=native_id,
                title_as_given=native_id,
                raw_payload={"raw": native_id},
                schema_version=version,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )

    engine = _patch_transaction(monkeypatch, conn)
    records, last_item_id, oversized = await normalize_replay.records_needing_replay(
        engine,
        provider_id="test",
        schema_version=2,  # type: ignore[arg-type]
        limit=1,
        max_record_bytes=1_024,
    )

    assert records == [RawRecord(native_id="stale", payload={"raw": "stale"})]
    assert last_item_id == 1
    assert oversized == 0

    records, last_item_id, oversized = await normalize_replay.records_needing_replay(
        engine,
        provider_id="test",
        schema_version=2,  # type: ignore[arg-type]
        after_item_id=last_item_id,
        limit=1,
        max_record_bytes=1_024,
    )
    assert records == [RawRecord(native_id="stale-again", payload={"raw": "stale-again"})]
    assert last_item_id == 3
    assert oversized == 0


async def test_replay_selection_skips_oversized_payload_and_keeps_scanning(
    conn: SyncConnectionAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for item_id, native_id, payload in (
        (1, "first", {"raw": "ok"}),
        (2, "large", {"raw": "x" * 500}),
        (3, "last", {"raw": "ok"}),
    ):
        await conn.execute(
            provider_items.insert().values(
                id=item_id,
                provider_id="test",
                native_id=native_id,
                title_as_given=native_id,
                raw_payload=payload,
                schema_version=1,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )

    engine = _patch_transaction(monkeypatch, conn)
    records, last_item_id, oversized = await normalize_replay.records_needing_replay(
        engine,
        provider_id="test",
        schema_version=2,  # type: ignore[arg-type]
        max_record_bytes=128,
    )

    assert records == [RawRecord(native_id="first", payload={"raw": "ok"})]
    assert last_item_id == 2
    assert oversized == 1

    records, last_item_id, oversized = await normalize_replay.records_needing_replay(
        engine,
        provider_id="test",
        schema_version=2,  # type: ignore[arg-type]
        after_item_id=last_item_id,
        max_record_bytes=128,
    )
    assert records == [RawRecord(native_id="last", payload={"raw": "ok"})]
    assert last_item_id == 3
    assert oversized == 0
