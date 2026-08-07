"""Regression tests for rejected-record envelopes and schema replay selection."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.schema import (
    entries,
    metadata,
    opinions,
    provider_items,
    sync_runs,
    works,
)
from aggregato.db.search import SearchKind, matching_ref_ids, sync_create_search_index
from aggregato.domain.enums import IngestStage, LoggedPrecision, MediaType, ReviewFormat
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
    records = await normalize_replay.records_needing_replay(
        engine,
        provider_id="test",
        schema_version=2,  # type: ignore[arg-type]
    )

    assert records == [RawRecord(native_id="stale", payload={"raw": "stale"})]


async def test_replay_derivative_cleanup_removes_old_review_documents(
    conn: SyncConnectionAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_id = uuid.uuid4()
    await conn.execute(
        works.insert().values(
            id=work_id,
            media_type=str(MediaType.FILM),
            title="Replay Work",
            sort_title="replay work",
            metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await conn.execute(
        provider_items.insert().values(
            id=1,
            provider_id="test",
            native_id="stale",
            work_id=work_id,
            title_as_given="Replay Work",
            raw_payload={},
            schema_version=1,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )
    await conn.execute(
        entries.insert().values(
            work_id=work_id,
            provider_id="test",
            provider_item_id=1,
            kind="watch",
            logged_at=NOW,
            logged_precision=str(LoggedPrecision.EXACT),
            ingested_at=NOW,
        )
    )
    await conn.execute(
        opinions.insert().values(
            work_id=work_id,
            provider_id="test",
            provider_item_id=1,
            review_text="old review text",
            review_format=str(ReviewFormat.PLAIN),
            updated_at=NOW,
        )
    )
    await conn.execute(
        text("INSERT INTO search_index (kind, ref_id, content) VALUES (:kind, :ref_id, :content)"),
        {"kind": str(SearchKind.REVIEW_TEXT), "ref_id": "1:0", "content": "old review text"},
    )
    assert await matching_ref_ids(conn, SearchKind.REVIEW_TEXT, "old review") == ["1:0"]

    engine = _patch_transaction(monkeypatch, conn)
    await normalize_replay.tombstone_replay_derivatives(
        engine, provider_id="test", native_ids=["stale"], now=NOW + timedelta(minutes=1)
    )

    assert await matching_ref_ids(conn, SearchKind.REVIEW_TEXT, "old review") == []
    assert (await conn.execute(select(entries.c.deleted_at))).scalar_one() is not None
    assert (await conn.execute(select(opinions.c.deleted_at))).scalar_one() is not None
