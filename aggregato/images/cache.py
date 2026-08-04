"""Content-addressed storage for provider-supplied artwork ."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aggregato.db.engine import transaction
from aggregato.db.schema import image_cache


def url_hash(url: str) -> str:
    return sha256(url.encode()).hexdigest()


async def register_source(engine: AsyncEngine, source_url: str) -> str:
    """Remember a provider-supplied image URL without fetching it during ingest."""
    async with transaction(engine) as conn:
        return await register_source_on_connection(conn, source_url)


async def register_source_on_connection(conn: AsyncConnection, source_url: str) -> str:
    """Register a source inside the writer transaction without fetching it during ingest."""
    digest = url_hash(source_url)
    existing = await conn.execute(
        select(image_cache.c.url_hash).where(image_cache.c.url_hash == digest)
    )
    if existing.first() is None:
        await conn.execute(image_cache.insert().values(url_hash=digest, source_url=source_url))
    return digest


async def cached_image(
    engine: AsyncEngine, data_dir: Path, digest: str, *, enabled: bool = True
) -> tuple[Path, str] | None:
    """Return a cached image, or ``None`` when caching is disabled or unavailable.

    The switch is checked before even looking up a source, keeping "images off" a true local
    placeholder mode rather than a mode that accidentally reaches the remote host.
    """
    if not enabled:
        return None
    async with transaction(engine) as conn:
        row = (
            await conn.execute(select(image_cache).where(image_cache.c.url_hash == digest))
        ).first()
    if row is None:
        return None
    if row.bytes_sha256 and row.content_type:
        path = _path(data_dir, row.bytes_sha256)
        if path.is_file():
            return path, row.content_type
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            response = await client.get(row.source_url)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "application/octet-stream").split(
            ";", 1
        )[0]
        if not content_type.startswith("image/"):
            raise ValueError(f"source returned {content_type}, not an image")
        bytes_hash = sha256(response.content).hexdigest()
        path = _path(data_dir, bytes_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    except (httpx.HTTPError, OSError, ValueError):
        async with transaction(engine) as conn:
            await conn.execute(
                image_cache.update()
                .where(image_cache.c.url_hash == digest)
                .values(failed_at=datetime.now(UTC), failure_count=image_cache.c.failure_count + 1)
            )
        return None
    async with transaction(engine) as conn:
        await conn.execute(
            image_cache.update()
            .where(image_cache.c.url_hash == digest)
            .values(
                bytes_sha256=bytes_hash,
                content_type=content_type,
                size_bytes=len(response.content),
                fetched_at=datetime.now(UTC),
                failed_at=None,
            )
        )
    return path, content_type


def _path(data_dir: Path, bytes_hash: str) -> Path:
    return data_dir / "images" / bytes_hash[:2] / bytes_hash
