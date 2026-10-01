"""Global archive settings and storage accounting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, LargeBinary, Text, cast, func, select
from starlette.requests import Request

from aggregato.api.clock import now as request_now
from aggregato.api.errors import ProblemError, error_type
from aggregato.db.engine import transaction
from aggregato.db.retention import get_settings, update_settings
from aggregato.db.schema import image_cache, ingest_failures, provider_items
from aggregato.export import sqlite_database_path

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
    image_cache_configured_enabled: bool
    backup_supported: bool
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
    cache_configured = request.app.state.config.image_cache_enabled
    values["image_cache_enabled"] = values["image_cache_enabled"] and cache_configured
    database_path = sqlite_database_path(request.app.state.config.database_url)
    return SettingsView(
        **values,
        image_cache_configured_enabled=cache_configured,
        backup_supported=database_path is not None,
        storage=await _storage(request, database_path),
    )


@router.patch("/settings", response_model=SettingsView)
async def patch_settings(request: Request, patch: SettingsPatch) -> SettingsView:
    values = patch.model_dump(exclude_none=True)
    cache_configured = request.app.state.config.image_cache_enabled
    if not cache_configured:
        values.pop("image_cache_enabled", None)
    try:
        updated = await update_settings(request.app.state.engine, values, now=request_now(request))
    except ValueError as exc:
        raise ProblemError(
            status=422,
            title="Invalid setting",
            detail=str(exc),
            type=error_type("invalid-setting"),
        ) from exc
    updated["image_cache_enabled"] = updated["image_cache_enabled"] and cache_configured
    database_path = sqlite_database_path(request.app.state.config.database_url)
    return SettingsView(
        **updated,
        image_cache_configured_enabled=cache_configured,
        backup_supported=database_path is not None,
        storage=await _storage(request, database_path),
    )


async def _storage(request: Request, database_path: Path | None) -> StorageUsage:
    engine = request.app.state.engine
    async with transaction(engine) as conn:
        raw = await conn.scalar(
            select(
                func.coalesce(
                    func.sum(_payload_byte_length(provider_items.c.raw_payload, conn.dialect.name)),
                    0,
                )
            )
        )
        failures = await conn.scalar(
            select(
                func.coalesce(
                    func.sum(
                        _payload_byte_length(ingest_failures.c.raw_payload, conn.dialect.name)
                    ),
                    0,
                )
            )
        )
        images = await conn.scalar(select(func.coalesce(func.sum(image_cache.c.size_bytes), 0)))
    db_size = _database_size(database_path)
    return StorageUsage(
        raw_payload_bytes=int(raw or 0) + int(failures or 0),
        image_cache_bytes=int(images or 0),
        database_bytes=db_size,
    )


def _database_size(database_path: Path | None) -> int:
    if database_path is None:
        return 0
    files = (database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm"))
    return sum(path.stat().st_size for path in files if path.is_file())


def _payload_byte_length(column: ColumnElement[Any], dialect_name: str) -> ColumnElement[Any]:
    """Count UTF-8 bytes in the JSON text rendering, not characters or database pages."""
    if dialect_name == "postgresql":
        return func.octet_length(cast(column, Text))
    return func.length(cast(column, LargeBinary))
