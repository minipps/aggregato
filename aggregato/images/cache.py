"""Bounded, content-addressed storage for provider-supplied artwork."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import tempfile
import threading
from collections.abc import Collection, Iterator, Sequence
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Final
from urllib.parse import SplitResult, urljoin, urlsplit
from weakref import WeakKeyDictionary

import httpx
import puremagic
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


def image_origin(url: str | httpx.URL) -> str:
    """Return the normalized ``scheme://host:port`` origin for an image source.

    The API uses this for the narrow exception to the private-address block: an operator may
    explicitly configure a provider whose artwork lives on a self-hosted service inside the
    deployment network. Paths, query strings, and fragments are intentionally excluded, so the
    exception can never turn one configured image path into permission to follow an arbitrary
    internal redirect.
    """
    return _origin(_parse_url(url))


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
    allowed_origins: Collection[str] = (),
) -> tuple[Path, str] | None:
    """Return a verified cached image, fetching it once when necessary.

    The setting and digest are checked before any source lookup. A per-digest lock then makes the
    database re-check, remote fetch, and local publication one operation within this process. The
    file itself is published atomically, so another process can only observe an old complete file
    or a new complete file.
    """
    if not enabled or not _is_digest(digest):
        return None

    allowed_origins = frozenset(allowed_origins)
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
                row.source_url, data_dir, allowed_origins=allowed_origins
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


async def _fetch_and_store(
    source_url: str,
    data_dir: Path,
    *,
    allowed_origins: Collection[str] = (),
) -> tuple[Path, str, str, int]:
    """Fetch one source through explicit, validated redirects and atomically store its bytes."""
    current_url = source_url
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=15,
        trust_env=False,
        headers={"Accept": "image/*"},
    ) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            address = await _validate_url_and_host(current_url, allowed_origins=allowed_origins)
            request_url, request_headers, request_extensions = _pin_request(current_url, address)
            async with client.stream(
                "GET",
                request_url,
                headers=request_headers,
                extensions=request_extensions,
                follow_redirects=False,
            ) as response:
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


async def _validate_url_and_host(
    url: str | httpx.URL, *, allowed_origins: Collection[str] = ()
) -> str:
    parsed = _parse_url(url)
    # DNS is blocking, but it must not run on the API loop. A daemon thread avoids making an
    # application-wide executor wait on a resolver that has stalled, while the future keeps the
    # validation result on the requesting loop.
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def resolve() -> None:
        try:
            address = _validate_resolved_host(parsed, allowed_origins=allowed_origins)
        except Exception as exc:
            loop.call_soon_threadsafe(_finish_resolution, future, None, exc)
        else:
            loop.call_soon_threadsafe(_finish_resolution, future, address, None)

    threading.Thread(target=resolve, name="aggregato-image-dns", daemon=True).start()
    return await future


def _finish_resolution(
    future: asyncio.Future[str], address: str | None, error: Exception | None
) -> None:
    if future.done():
        return
    if error is None:
        assert address is not None
        future.set_result(address)
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
    return parsed


def _validate_resolved_host(parsed: SplitResult, *, allowed_origins: Collection[str] = ()) -> str:
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

    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    private_origin_allowed = _origin(parsed) in allowed_origins
    for address in addresses:
        found.append(address)
        if _unsafe_address(address) and not private_origin_allowed:
            raise UnsafeImageURL("image URL resolves to a non-public address")
    if not found:
        raise UnsafeImageURL("image URL host has no addresses")
    # Returning the validated literal is important. HTTPX's resolver is otherwise free to resolve
    # the hostname again after this check, which creates a DNS rebinding/TOCTOU window.
    return str(found[0])


def _origin(parsed: SplitResult) -> str:
    hostname = parsed.hostname
    assert hostname is not None  # _parse_url checks this first
    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or _DEFAULT_PORTS[parsed.scheme.lower()]
    return f"{parsed.scheme.lower()}://{host}:{port}"


def _pin_request(
    url: str | httpx.URL, address: str
) -> tuple[httpx.URL, dict[str, str], dict[str, str]]:
    """Connect to a checked address while retaining the origin host for HTTP and TLS."""
    parsed = _parse_url(url)
    pinned_url = httpx.URL(str(url)).copy_with(host=address)
    host = parsed.hostname
    assert host is not None  # _parse_url checks this
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    default_port = _DEFAULT_PORTS[parsed.scheme.lower()]
    host_header = host if port in (None, default_port) else f"{host}:{port}"
    return pinned_url, {"Host": host_header}, {"sni_hostname": host}


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
            size_bytes = 0
            async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                if size_bytes + len(chunk) > MAX_IMAGE_BYTES:
                    raise ValueError("image response is too large")
                temporary.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())

        assert temporary_path is not None
        content_type = _detect_image_type(temporary_path)
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
    """Verify the bounded cached file and identify it from its magic bytes."""
    if not path.is_file() or path.is_symlink():
        return None
    digest = sha256()
    size_bytes = 0
    try:
        with path.open("rb") as cached:
            while chunk := cached.read(_CHUNK_SIZE):
                if size_bytes + len(chunk) > MAX_IMAGE_BYTES:
                    return None
                digest.update(chunk)
                size_bytes += len(chunk)
    except OSError:
        return None
    if digest.hexdigest() != expected_hash or (
        expected_size is not None and size_bytes != expected_size
    ):
        return None
    return _detect_image_type(path)


_SUPPORTED_IMAGE_TYPES: Final = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/heic",
        "image/ico",
        "image/jp2",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
        "image/x-icon",
    }
)
"""Raster image MIME types accepted by the cache after magic-byte detection."""

_EXTENSION_IMAGE_TYPES: Final = {
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".dib": "image/bmp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heic",
    ".ico": "image/x-icon",
    ".jfif": "image/jpeg",
    ".jp2": "image/jp2",
    ".jpf": "image/jp2",
    ".jpm": "image/jp2",
    ".jpx": "image/jp2",
    ".jpe": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}
"""MIME aliases for formats where puremagic's MIME label is generic or platform-specific."""


def _detect_image_type(path: Path) -> str | None:
    """Identify a file with puremagic, accepting only known raster formats."""
    try:
        matches = puremagic.magic_file(path)
    except (OSError, ValueError, puremagic.PureError):
        return None
    for match in matches:
        extension = str(match.extension).lower()
        mapped = _EXTENSION_IMAGE_TYPES.get(extension)
        if mapped is not None:
            return mapped
        mime_type = str(match.mime_type).lower()
        if mime_type in _SUPPORTED_IMAGE_TYPES:
            return mime_type
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
