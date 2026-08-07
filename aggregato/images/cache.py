"""Bounded, content-addressed storage for provider-supplied artwork."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import tempfile
import threading
from collections.abc import Iterator, Sequence
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Final
from urllib.parse import SplitResult, urljoin, urlsplit
from weakref import WeakKeyDictionary

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aggregato.db.engine import transaction
from aggregato.db.schema import image_cache
from aggregato.domain.clock import SYSTEM_CLOCK, Clock

MAX_IMAGE_BYTES: Final = 10 * 1024 * 1024
"""Maximum number of response bytes retained for one image."""

MAX_REDIRECTS: Final = 5
"""Maximum number of explicitly validated redirect hops for one image."""

_CHUNK_SIZE: Final = 64 * 1024
_MAGIC_PREFIX_BYTES: Final = 4096
_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}

_DigestLocks = WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]
_digest_locks: _DigestLocks = WeakKeyDictionary()
_digest_locks_guard = threading.Lock()


class UnsafeImageURL(ValueError):
    """Raised when an image URL or one of its resolved addresses is not fetchable safely."""


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
    engine: AsyncEngine,
    data_dir: Path,
    digest: str,
    *,
    enabled: bool = True,
    now: datetime | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> tuple[Path, str] | None:
    """Return a verified cached image, fetching it once when necessary.

    The setting and digest are checked before any source lookup. A per-digest lock then makes the
    database re-check, remote fetch, and local publication one operation within this process. The
    file itself is published atomically, so another process can only observe an old complete file
    or a new complete file.
    """
    if not enabled or not _is_digest(digest):
        return None

    async with _digest_lock(digest):
        async with transaction(engine) as conn:
            row = (
                await conn.execute(select(image_cache).where(image_cache.c.url_hash == digest))
            ).first()
        if row is None:
            return None

        path = (
            _path(data_dir, row.bytes_sha256)
            if row.bytes_sha256 and _is_digest(row.bytes_sha256)
            else None
        )
        if path is not None and row.content_type:
            cached_type = _verified_cached_type(path, row.bytes_sha256, row.size_bytes)
            if cached_type is not None:
                return path, cached_type

        try:
            path, content_type, bytes_hash, size_bytes = await _fetch_and_store(
                row.source_url, data_dir
            )
        except (httpx.HTTPError, OSError, TimeoutError, UnsafeImageURL, ValueError):
            failed_at = now or clock.now()
            async with transaction(engine) as conn:
                await conn.execute(
                    image_cache.update()
                    .where(image_cache.c.url_hash == digest)
                    .values(
                        failed_at=failed_at,
                        failure_count=image_cache.c.failure_count + 1,
                    )
                )
            return None

        fetched_at = now or clock.now()
        async with transaction(engine) as conn:
            await conn.execute(
                image_cache.update()
                .where(image_cache.c.url_hash == digest)
                .values(
                    bytes_sha256=bytes_hash,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    fetched_at=fetched_at,
                    failed_at=None,
                )
            )
        return path, content_type


async def _fetch_and_store(source_url: str, data_dir: Path) -> tuple[Path, str, str, int]:
    """Fetch one source through explicit, validated redirects and atomically store its bytes."""
    current_url = source_url
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=15,
        trust_env=False,
        headers={"Accept": "image/*"},
        event_hooks={"request": [_revalidate_request]},
    ) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            await _validate_url_and_host(current_url)
            async with client.stream("GET", current_url, follow_redirects=False) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    if redirect_count == MAX_REDIRECTS:
                        raise UnsafeImageURL("too many image redirects")
                    location = response.headers.get("location")
                    if not location:
                        raise UnsafeImageURL("image redirect has no location")
                    current_url = urljoin(current_url, location)
                    continue

                response.raise_for_status()
                return await _store_response(response, data_dir)

    raise UnsafeImageURL("image redirect chain did not terminate")


async def _revalidate_request(request: httpx.Request) -> None:
    """Re-check the URL immediately before HTTPX opens a request connection."""
    await _validate_url_and_host(request.url)


async def _validate_url_and_host(url: str | httpx.URL) -> None:
    parsed = _parse_url(url)
    # DNS is blocking, but it must not run on the API loop. A daemon thread avoids making an
    # application-wide executor wait on a resolver that has stalled, while the future keeps the
    # validation result on the requesting loop.
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()

    def resolve() -> None:
        try:
            _validate_resolved_host(parsed)
        except Exception as exc:
            loop.call_soon_threadsafe(_finish_resolution, future, exc)
        else:
            loop.call_soon_threadsafe(_finish_resolution, future, None)

    threading.Thread(target=resolve, name="aggregato-image-dns", daemon=True).start()
    await future


def _finish_resolution(future: asyncio.Future[None], error: Exception | None) -> None:
    if future.done():
        return
    if error is None:
        future.set_result(None)
    else:
        future.set_exception(error)


def _parse_url(url: str | httpx.URL) -> SplitResult:
    raw = str(url)
    if len(raw) > 2048 or any(ord(char) < 0x20 for char in raw):
        raise UnsafeImageURL("image URL is malformed")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeImageURL("image URL is malformed") from exc
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeImageURL("image URL scheme is not allowed")
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise UnsafeImageURL("image URL must have a host and no credentials")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeImageURL("image URL port is invalid")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise UnsafeImageURL("image URL host is local")
    return parsed


def _validate_resolved_host(parsed: SplitResult) -> None:
    hostname = parsed.hostname
    if hostname is None:  # pragma: no cover - _parse_url checks this first
        raise UnsafeImageURL("image URL has no host")
    try:
        addresses: Iterator[ipaddress.IPv4Address | ipaddress.IPv6Address] = iter(
            [ipaddress.ip_address(hostname)]
        )
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                hostname,
                parsed.port or _DEFAULT_PORTS[parsed.scheme.lower()],
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise UnsafeImageURL("image URL host could not be resolved") from exc
        addresses = _resolved_addresses(resolved)

    found = False
    for address in addresses:
        found = True
        if _unsafe_address(address):
            raise UnsafeImageURL("image URL resolves to a non-public address")
    if not found:
        raise UnsafeImageURL("image URL host has no addresses")


def _resolved_addresses(
    resolved: Sequence[tuple[object, object, object, object, tuple[object, ...]]],
) -> Iterator[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    for result in resolved:
        sockaddr = result[4]
        if not sockaddr:
            raise UnsafeImageURL("image URL host has an invalid address")
        try:
            yield ipaddress.ip_address(str(sockaddr[0]))
        except (IndexError, ValueError) as exc:
            raise UnsafeImageURL("image URL host has an invalid address") from exc


def _unsafe_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject every address that is not globally routable, including mapped/reserved ranges."""
    return (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    )


async def _store_response(response: httpx.Response, data_dir: Path) -> tuple[Path, str, str, int]:
    declared_length = response.headers.get("content-length")
    if declared_length is not None and (
        not declared_length.isdigit() or int(declared_length) > MAX_IMAGE_BYTES
    ):
        raise ValueError("image response is too large")

    declared_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not declared_type.startswith("image/") or "svg" in declared_type:
        raise ValueError("image response content type is not a safe raster image")

    images_dir = data_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=images_dir, prefix=".image-", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            digest = sha256()
            prefix = bytearray()
            size_bytes = 0
            async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                if size_bytes + len(chunk) > MAX_IMAGE_BYTES:
                    raise ValueError("image response is too large")
                temporary.write(chunk)
                digest.update(chunk)
                if len(prefix) < _MAGIC_PREFIX_BYTES:
                    prefix.extend(chunk[: _MAGIC_PREFIX_BYTES - len(prefix)])
                size_bytes += len(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())

        content_type = _detect_image_type(bytes(prefix))
        if content_type is None:
            raise ValueError("image response has invalid or unsupported magic bytes")

        bytes_hash = digest.hexdigest()
        path = _path(data_dir, bytes_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
        if _verified_cached_type(path, bytes_hash, size_bytes) != content_type:
            path.unlink(missing_ok=True)
            raise OSError("stored image hash verification failed")
        return path, content_type, bytes_hash, size_bytes
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _verified_cached_type(path: Path, expected_hash: str, expected_size: int | None) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = sha256()
    prefix = bytearray()
    size_bytes = 0
    try:
        with path.open("rb") as cached:
            while chunk := cached.read(_CHUNK_SIZE):
                digest.update(chunk)
                if len(prefix) < _MAGIC_PREFIX_BYTES:
                    prefix.extend(chunk[: _MAGIC_PREFIX_BYTES - len(prefix)])
                size_bytes += len(chunk)
    except OSError:
        return None
    if digest.hexdigest() != expected_hash or (
        expected_size is not None and size_bytes != expected_size
    ):
        return None
    return _detect_image_type(bytes(prefix))


def _detect_image_type(prefix: bytes) -> str | None:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"BM"):
        return "image/bmp"
    if prefix[:4] in {b"II*\x00", b"MM\x00*"}:
        return "image/tiff"
    if prefix.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if prefix.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n"):
        return "image/jp2"
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        brand = prefix[8:12]
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
    return None


def _digest_lock(digest: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _digest_locks_guard:
        locks = _digest_locks.setdefault(loop, {})
        return locks.setdefault(digest, asyncio.Lock())


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _fsync_directory(directory: Path) -> None:
    """Persist the rename where the platform supports directory fsync."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        os.close(descriptor)


def _path(data_dir: Path, bytes_hash: str) -> Path:
    return data_dir / "images" / bytes_hash[:2] / bytes_hash
