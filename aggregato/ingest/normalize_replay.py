"""Read bounded batches of retained payloads whose normalizer version is stale."""

from __future__ import annotations

import json
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import LargeBinary, String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import transaction
from aggregato.db.schema import provider_items
from aggregato.domain.models import RawRecord


async def records_needing_replay(
    engine: AsyncEngine,
    *,
    provider_id: str,
    schema_version: int,
    after_item_id: int | None = None,
    limit: int = 1_000,
    max_record_bytes: int,
) -> tuple[list[RawRecord], int | None, int]:
    """Read stale records after ``after_item_id`` within count and encoded-byte limits.

    Returns the selected raws, the last item id examined, and the number skipped because a single
    raw could not fit the child request. Skipped items stay stale for the next run. The database
    checks serialized payload size before returning payload values, so one oversized row cannot
    make the parent load an unbounded request into memory.
    """
    if limit < 1:
        raise ValueError("replay batch limit must be positive")

    conditions = [
        provider_items.c.provider_id == provider_id,
        provider_items.c.schema_version < schema_version,
    ]
    if after_item_id is not None:
        conditions.append(provider_items.c.id > after_item_id)

    records: list[RawRecord] = []
    used_bytes = 0
    used_database_bytes = 0
    oversized = 0
    last_item_id = after_item_id
    async with transaction(engine) as conn:
        serialized_payload = cast(provider_items.c.raw_payload, String)
        if conn.dialect.name == "sqlite":
            payload_size = func.length(cast(serialized_payload, LargeBinary))
            native_id_size = func.length(cast(provider_items.c.native_id, LargeBinary))
        else:
            payload_size = func.octet_length(serialized_payload)
            native_id_size = func.octet_length(provider_items.c.native_id)
        candidates = await conn.execute(
            select(
                provider_items.c.id,
                payload_size.label("payload_bytes"),
                native_id_size.label("native_id_bytes"),
            )
            .where(*conditions)
            .order_by(provider_items.c.id)
            .limit(limit)
        )
        rows = list(candidates)
        selected_ids: set[int] = set()
        oversized_ids: set[int] = set()
        for candidate in rows:
            item_id = int(candidate.id)
            stored_bytes = int(candidate.payload_bytes or 0) + int(candidate.native_id_bytes or 0)
            # Leave room for RawRecord's JSON fields and separators. The exact escaped size is
            # checked after this one bounded payload query; PostgreSQL JSON text may use Unicode
            # that expands when json.dumps produces the child request.
            stored_bytes += 64
            if stored_bytes > max_record_bytes:
                oversized_ids.add(item_id)
                continue
            if selected_ids and used_database_bytes + stored_bytes > max_record_bytes:
                break
            selected_ids.add(item_id)
            used_database_bytes += stored_bytes

        payloads: dict[int, tuple[str, dict[str, Any]]] = {}
        if selected_ids:
            payload_rows = await conn.execute(
                select(
                    provider_items.c.id,
                    provider_items.c.native_id,
                    provider_items.c.raw_payload,
                ).where(provider_items.c.id.in_(selected_ids))
            )
            payloads = {
                int(row.id): (str(row.native_id), typing_cast(dict[str, Any], row.raw_payload))
                for row in payload_rows
            }

        for candidate in rows:
            item_id = int(candidate.id)
            if item_id in oversized_ids:
                oversized += 1
                last_item_id = item_id
                continue
            if item_id not in payloads:
                break
            native_id, payload = payloads[item_id]
            record = RawRecord(native_id=native_id, payload=payload)
            size = len(json.dumps(record.model_dump(mode="json")).encode())
            if size > max_record_bytes:
                oversized += 1
                last_item_id = item_id
                continue
            if records and used_bytes + size + 1 > max_record_bytes:
                break
            records.append(record)
            used_bytes += size + (1 if len(records) > 1 else 0)
            last_item_id = item_id

    return records, last_item_id, oversized
