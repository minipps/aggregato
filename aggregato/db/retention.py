"""Retention settings and cleanup for locally retained operational data ."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, TypedDict, cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aggregato.db.engine import transaction
from aggregato.db.schema import image_cache, ingest_failures, settings, sync_runs

RAW_PAYLOAD_RETENTION_DAYS: Final = 90
SUCCESS_RUN_RETENTION_DAYS: Final = 30
FAILURE_RUN_RETENTION_DAYS: Final = 180
IMAGE_CACHE_ENABLED: Final = True


class RetentionSettings(TypedDict):
    raw_payload_retention_days: int
    success_run_retention_days: int
    failure_run_retention_days: int
    image_cache_enabled: bool


_DEFAULTS: Final[RetentionSettings] = {
    "raw_payload_retention_days": RAW_PAYLOAD_RETENTION_DAYS,
    "success_run_retention_days": SUCCESS_RUN_RETENTION_DAYS,
    "failure_run_retention_days": FAILURE_RUN_RETENTION_DAYS,
    "image_cache_enabled": IMAGE_CACHE_ENABLED,
}


async def get_settings(engine: AsyncEngine) -> RetentionSettings:
    async with transaction(engine) as conn:
        return await get_settings_on_connection(conn)


async def get_settings_on_connection(conn: AsyncConnection) -> RetentionSettings:
    result = await conn.execute(select(settings.c.key, settings.c.value))
    values = dict(_DEFAULTS)
    for row in result:
        if row.key in values:
            values[row.key] = row.value
    return RetentionSettings(
        raw_payload_retention_days=cast(int, values["raw_payload_retention_days"]),
        success_run_retention_days=cast(int, values["success_run_retention_days"]),
        failure_run_retention_days=cast(int, values["failure_run_retention_days"]),
        image_cache_enabled=bool(values["image_cache_enabled"]),
    )


async def update_settings(engine: AsyncEngine, values: dict[str, int | bool]) -> RetentionSettings:
    unknown = set(values) - set(_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    _validate(values)
    now = datetime.now(UTC)
    async with transaction(engine) as conn:
        for key, value in values.items():
            existing = await conn.scalar(select(settings.c.key).where(settings.c.key == key))
            if existing is None:
                await conn.execute(settings.insert().values(key=key, value=value, updated_at=now))
            else:
                await conn.execute(
                    settings.update()
                    .where(settings.c.key == key)
                    .values(value=value, updated_at=now)
                )
        return await get_settings_on_connection(conn)


def _validate(values: dict[str, int | bool]) -> None:
    for key, value in values.items():
        if key.endswith("_days") and (type(value) is not int or value < 0):
            raise ValueError(f"{key} must be a non-negative whole number of days")
        if key == "image_cache_enabled" and not isinstance(value, bool):
            raise ValueError("image_cache_enabled must be true or false")


async def cleanup(engine: AsyncEngine, data_dir: Path, *, now: datetime | None = None) -> None:
    """Remove expired operational records and disabled-cache files.

    Failed runs are retained six times longer than successful ones by default: they are the useful
    evidence when a provider misbehaves.  Provider payloads are never silently purged unless the
    operator explicitly lowers their retention setting.
    """
    now = now or datetime.now(UTC)
    values = await get_settings(engine)
    async with transaction(engine) as conn:
        await conn.execute(
            delete(sync_runs).where(
                sync_runs.c.status == "success",
                sync_runs.c.finished_at
                < now - timedelta(days=int(values["success_run_retention_days"])),
            )
        )
        await conn.execute(
            delete(sync_runs).where(
                sync_runs.c.status.in_(("failed", "partial")),
                sync_runs.c.finished_at
                < now - timedelta(days=int(values["failure_run_retention_days"])),
            )
        )
        await conn.execute(
            delete(ingest_failures).where(
                ingest_failures.c.created_at
                < now - timedelta(days=int(values["raw_payload_retention_days"])),
                ingest_failures.c.resolved_at.is_not(None),
            )
        )
        if not values["image_cache_enabled"]:
            await conn.execute(delete(image_cache))
    if not values["image_cache_enabled"]:
        image_root = data_dir / "images"
        if image_root.exists():
            for path in sorted(image_root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
