"""Global archive settings and storage accounting ."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from starlette.requests import Request

from aggregato.api.clock import now as request_now
from aggregato.api.errors import ProblemError, error_type
from aggregato.db.engine import transaction
from aggregato.db.retention import get_settings, update_settings
from aggregato.db.schema import image_cache, ingest_failures, provider_items

router = APIRouter(tags=["operations"])


class StorageUsage(BaseModel):
    raw_payload_bytes: int
    image_cache_bytes: int
    database_bytes: int


class SettingsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_payload_retention_days: int
    success_run_retention_days: int
    failure_run_retention_days: int
    image_cache_enabled: bool
    storage: StorageUsage


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_payload_retention_days: int | None = Field(default=None, ge=0)
    success_run_retention_days: int | None = Field(default=None, ge=0)
    failure_run_retention_days: int | None = Field(default=None, ge=0)
    image_cache_enabled: bool | None = None


@router.get("/settings", response_model=SettingsView)
async def read_settings(request: Request) -> SettingsView:
    values = await get_settings(request.app.state.engine)
    return SettingsView(**values, storage=await _storage(request))


@router.patch("/settings", response_model=SettingsView)
async def patch_settings(request: Request, patch: SettingsPatch) -> SettingsView:
    values = patch.model_dump(exclude_none=True)
    try:
        updated = await update_settings(request.app.state.engine, values, now=request_now(request))
    except ValueError as exc:
        raise ProblemError(
            status=422,
            title="Invalid setting",
            detail=str(exc),
            type=error_type("invalid-setting"),
        ) from exc
    return SettingsView(**updated, storage=await _storage(request))


async def _storage(request: Request) -> StorageUsage:
    engine = request.app.state.engine
    async with transaction(engine) as conn:
        # JSON encoding differs by dialect, but character length provides an honest, portable
        # retained-payload estimate rather than pretending the database page count is data size.
        raw = await conn.scalar(
            select(func.coalesce(func.sum(func.length(provider_items.c.raw_payload)), 0))
        )
        failures = await conn.scalar(
            select(func.coalesce(func.sum(func.length(ingest_failures.c.raw_payload)), 0))
        )
        images = await conn.scalar(select(func.coalesce(func.sum(image_cache.c.size_bytes), 0)))
    db_size = _database_size(request.app.state.config.data_dir)
    return StorageUsage(
        raw_payload_bytes=int(raw or 0) + int(failures or 0),
        image_cache_bytes=int(images or 0),
        database_bytes=db_size,
    )


def _database_size(data_dir: Path) -> int:
    return sum(path.stat().st_size for path in data_dir.glob("aggregato.db*") if path.is_file())
