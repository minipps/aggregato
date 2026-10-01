"""Authenticated snapshots of the transient playback state."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.api.deps import require_websocket_auth
from aggregato.api.errors import ProblemError
from aggregato.api.queries import aware
from aggregato.api.schemas import image_path
from aggregato.db.engine import transaction
from aggregato.db.schema import provider_state, providers
from aggregato.domain.enums import Capability, Confidence, CreatorKind, MediaType, Role
from aggregato.domain.models import NowPlayingItem as DomainNowPlayingItem
from aggregato.providers.registry import discover_providers

router = APIRouter(tags=["operations"])

STALE_AFTER = timedelta(seconds=45)


class NowPlayingWork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: MediaType
    title: str
    original_title: str | None = None
    release_year: int | None = None
    sequence_number: int | None = None
    image: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NowPlayingCredit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator_name: str
    creator_kind: CreatorKind
    role: Role
    role_raw: str
    credited_as: str | None = None
    position: int


class NowPlayingExternalId(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str
    value: str
    confidence: Confidence


class NowPlayingCreatorExternalId(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator_name: str
    namespace: str
    value: str
    confidence: Confidence


class NowPlayingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    changed_at: datetime
    work: NowPlayingWork
    credits: list[NowPlayingCredit] = Field(default_factory=list)
    external_ids: list[NowPlayingExternalId] = Field(default_factory=list)
    creator_external_ids: list[NowPlayingCreatorExternalId] = Field(default_factory=list)


class NowPlayingSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["snapshot"] = "snapshot"
    generated_at: datetime
    items: list[NowPlayingItem] = Field(default_factory=list)


@router.websocket("/ws/now-playing")
async def now_playing_socket(websocket: WebSocket) -> None:
    """Stream current provider playback, suppressing snapshots with no semantic change."""
    try:
        await require_websocket_auth(websocket)
    except ProblemError:
        # WebSocket routes do not pass through the HTTP problem-detail handlers.  Match the sync
        # websocket's policy close for every failed authentication handshake.
        await websocket.close(code=1008, reason="authentication required")
        return

    capable_ids = {
        info.id
        for info in discover_providers(websocket.app.state.config.provider_dir)
        if Capability.NOW_PLAYING.value in info.capabilities
    }
    await websocket.accept()
    previous_state: str | None = None
    try:
        while True:
            snapshot = await _snapshot(
                websocket.app.state.engine,
                capable_ids=capable_ids,
                generated_at=websocket.app.state.clock.now(),
            )
            state = json.dumps(
                snapshot.model_dump(mode="json", exclude={"generated_at"}),
                separators=(",", ":"),
                sort_keys=True,
            )
            if state != previous_state:
                await websocket.send_text(
                    json.dumps(
                        snapshot.model_dump(mode="json"),
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                previous_state = state
            try:
                async with asyncio.timeout(0.5):
                    while (await websocket.receive())["type"] != "websocket.disconnect":
                        pass
            except TimeoutError:
                continue
            return
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        return


async def _snapshot(
    engine: AsyncEngine,
    *,
    capable_ids: set[str],
    generated_at: datetime,
) -> NowPlayingSnapshot:
    """Read and validate fresh playback state in one transaction."""
    generated_at = aware(generated_at) or generated_at.replace(tzinfo=UTC)
    if not capable_ids:
        return NowPlayingSnapshot(generated_at=generated_at)

    statement = (
        select(
            providers.c.id,
            provider_state.c.now_playing_item,
            provider_state.c.now_playing_changed_at,
            provider_state.c.now_playing_checked_at,
        )
        .select_from(providers.join(provider_state, provider_state.c.provider_id == providers.c.id))
        .where(providers.c.id.in_(capable_ids), providers.c.enabled.is_(True))
        .order_by(providers.c.id)
    )
    cutoff = generated_at - STALE_AFTER
    async with transaction(engine) as conn:
        rows = list(await conn.execute(statement))

    items: list[NowPlayingItem] = []
    for row in rows:
        checked_at = aware(row.now_playing_checked_at)
        changed_at = aware(row.now_playing_changed_at)
        if (
            row.now_playing_item is None
            or checked_at is None
            or changed_at is None
            or checked_at < cutoff
        ):
            continue
        try:
            item = DomainNowPlayingItem.model_validate(row.now_playing_item)
        except ValidationError:
            continue
        items.append(_view(str(row.id), changed_at, item))
    return NowPlayingSnapshot(generated_at=generated_at, items=items)


def _view(provider_id: str, changed_at: datetime, item: DomainNowPlayingItem) -> NowPlayingItem:
    work = item.work
    return NowPlayingItem(
        provider_id=provider_id,
        changed_at=changed_at,
        work=NowPlayingWork(
            media_type=work.media_type,
            title=work.title,
            original_title=work.original_title,
            release_year=work.release_year,
            sequence_number=work.sequence_number,
            image=image_path(work.image_url),
            metadata=work.metadata,
        ),
        credits=[
            NowPlayingCredit(
                creator_name=credit.creator_name,
                creator_kind=credit.creator_kind,
                role=credit.role,
                role_raw=credit.role_raw,
                credited_as=credit.credited_as,
                position=credit.position,
            )
            for credit in item.credits
        ],
        external_ids=[
            NowPlayingExternalId(
                namespace=external_id.namespace,
                value=external_id.value,
                confidence=external_id.confidence,
            )
            for external_id in item.external_ids
        ],
        creator_external_ids=[
            NowPlayingCreatorExternalId(
                creator_name=external_id.creator_name,
                namespace=external_id.namespace,
                value=external_id.value,
                confidence=external_id.confidence,
            )
            for external_id in item.creator_external_ids
        ],
    )
