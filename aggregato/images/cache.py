"""Bounded, content-addressed storage for provider-supplied artwork."""

from __future__ import annotations

import asyncio
import binascii
import ipaddress
import os
import socket
import struct
import tempfile
import threading
import zlib
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
_MAX_DECODED_IMAGE_BYTES: Final = 64 * 1024 * 1024
_MAX_IMAGE_PIXELS: Final = 100_000_000

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
    ) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            address = await _validate_url_and_host(current_url)
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


async def _validate_url_and_host(url: str | httpx.URL) -> str:
    parsed = _parse_url(url)
    # DNS is blocking, but it must not run on the API loop. A daemon thread avoids making an
    # application-wide executor wait on a resolver that has stalled, while the future keeps the
    # validation result on the requesting loop.
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def resolve() -> None:
        try:
            address = _validate_resolved_host(parsed)
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
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise UnsafeImageURL("image URL host is local")
    return parsed


def _validate_resolved_host(parsed: SplitResult) -> str:
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
    for address in addresses:
        found.append(address)
        if _unsafe_address(address):
            raise UnsafeImageURL("image URL resolves to a non-public address")
    if not found:
        raise UnsafeImageURL("image URL host has no addresses")
    # Returning the validated literal is important. HTTPX's resolver is otherwise free to resolve
    # the hostname again after this check, which creates a DNS rebinding/TOCTOU window.
    return str(found[0])


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
        if content_type is None or not _is_decodable_file(temporary_path, content_type, size_bytes):
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
    body = bytearray()
    size_bytes = 0
    try:
        with path.open("rb") as cached:
            while chunk := cached.read(_CHUNK_SIZE):
                if size_bytes + len(chunk) > MAX_IMAGE_BYTES:
                    return None
                digest.update(chunk)
                if len(prefix) < _MAGIC_PREFIX_BYTES:
                    prefix.extend(chunk[: _MAGIC_PREFIX_BYTES - len(prefix)])
                body.extend(chunk)
                size_bytes += len(chunk)
    except OSError:
        return None
    if digest.hexdigest() != expected_hash or (
        expected_size is not None and size_bytes != expected_size
    ):
        return None
    content_type = _detect_image_type(bytes(prefix))
    if content_type is None or not _is_decodable_image(bytes(body), content_type):
        return None
    return content_type


def _is_decodable_file(path: Path, content_type: str, expected_size: int) -> bool:
    """Validate a bounded temporary image without ever reading an unbounded local file."""
    body = bytearray()
    try:
        with path.open("rb") as image_file:
            while chunk := image_file.read(_CHUNK_SIZE):
                if len(body) + len(chunk) > MAX_IMAGE_BYTES:
                    return False
                body.extend(chunk)
    except OSError:
        return False
    return len(body) == expected_size and _is_decodable_image(bytes(body), content_type)


def _is_decodable_image(body: bytes, content_type: str) -> bool:
    """Validate the bounded structure of every image format recognized by the cache."""
    try:
        if content_type == "image/png":
            return _decode_png(body)
        if content_type == "image/jpeg":
            return _decode_jpeg(body)
        if content_type == "image/gif":
            return _decode_gif(body)
        if content_type == "image/webp":
            return _decode_webp(body)
        if content_type == "image/bmp":
            return _decode_bmp(body)
        if content_type == "image/tiff":
            return _decode_tiff(body)
        if content_type == "image/x-icon":
            return _decode_ico(body)
        if content_type == "image/jp2":
            return _decode_jp2(body)
        if content_type in {"image/avif", "image/heic"}:
            return _decode_isobmff_image(body, content_type)
    except (IndexError, struct.error, ValueError, zlib.error):
        return False
    return False


def _decode_png(body: bytes) -> bool:
    signature = b"\x89PNG\r\n\x1a\n"
    if not body.startswith(signature):
        return False

    offset = len(signature)
    ihdr: tuple[int, int, int, int] | None = None
    palette_entries: int | None = None
    idat = bytearray()
    saw_idat = False
    saw_iend = False
    previous_was_idat = False
    while offset < len(body):
        if offset + 12 > len(body):
            return False
        length = struct.unpack_from(">I", body, offset)[0]
        chunk_type = body[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(body) or len(chunk_type) != 4:
            return False
        chunk = body[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack_from(">I", body, offset + 8 + length)[0]
        if binascii.crc32(chunk_type + chunk) & 0xFFFFFFFF != expected_crc:
            return False
        if not all((65 <= byte <= 90) or (97 <= byte <= 122) for byte in chunk_type):
            return False

        if ihdr is None and chunk_type != b"IHDR":
            return False
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13 or offset != len(signature):
                return False
            (
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filter_method,
                interlace,
            ) = struct.unpack(">IIBBBBB", chunk)
            if (
                width == 0
                or height == 0
                or width * height > _MAX_IMAGE_PIXELS
                or compression != 0
                or filter_method != 0
                or interlace != 0
            ):
                return False
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if bit_depth not in valid_depths.get(color_type, set()):
                return False
            ihdr = (width, height, bit_depth, color_type)
        elif chunk_type == b"PLTE":
            if ihdr is None or saw_idat or length == 0 or length % 3 != 0 or length > 768:
                return False
            palette_entries = length // 3
        elif chunk_type == b"IDAT":
            if ihdr is None or saw_iend or (saw_idat and not previous_was_idat):
                return False
            saw_idat = True
            idat.extend(chunk)
            previous_was_idat = True
        elif chunk_type == b"IEND":
            if length != 0 or ihdr is None or not saw_idat or saw_iend:
                return False
            saw_iend = True
            previous_was_idat = False
        else:
            if saw_iend or (chunk_type[0] & 0x20) == 0:
                return False
            previous_was_idat = False

        offset = end

    if ihdr is None or not saw_idat or not saw_iend or offset != len(body):
        return False
    width, height, bit_depth, color_type = ihdr
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    row_bytes = (width * channels * bit_depth + 7) // 8
    decoded_bytes = (row_bytes + 1) * height
    if decoded_bytes > _MAX_DECODED_IMAGE_BYTES:
        return False
    if color_type == 3 and palette_entries is None:
        return False
    if color_type == 3 and palette_entries is not None and palette_entries > 1 << bit_depth:
        return False

    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(idat), decoded_bytes + 1)
        if len(decoded) <= decoded_bytes:
            decoded += decompressor.flush(decoded_bytes + 1 - len(decoded))
    except (ValueError, zlib.error):
        return False
    if (
        len(decoded) != decoded_bytes
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        return False

    bytes_per_pixel = max(1, (channels * bit_depth + 7) // 8)
    previous = bytearray(row_bytes)
    position = 0
    for _ in range(height):
        filter_type = decoded[position]
        position += 1
        row = bytearray(decoded[position : position + row_bytes])
        position += row_bytes
        if filter_type == 1:
            for index in range(row_bytes):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + left) & 0xFF
        elif filter_type == 2:
            for index in range(row_bytes):
                row[index] = (row[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(row_bytes):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + (left + previous[index]) // 2) & 0xFF
        elif filter_type == 4:
            for index in range(row_bytes):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                upper = previous[index]
                upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + _png_paeth(left, upper, upper_left)) & 0xFF
        elif filter_type != 0:
            return False
        previous = row

    return position == len(decoded)


def _png_paeth(left: int, upper: int, upper_left: int) -> int:
    estimate = left + upper - upper_left
    left_distance = abs(estimate - left)
    upper_distance = abs(estimate - upper)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= upper_distance and left_distance <= upper_left_distance:
        return left
    if upper_distance <= upper_left_distance:
        return upper
    return upper_left


_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)


def _decode_jpeg(body: bytes) -> bool:
    """Perform a bounded JPEG marker/table/entropy preflight.

    The standard library does not ship a JPEG pixel decoder. This checks the complete container,
    frame dimensions, quantization/Huffman references, scan boundaries, byte stuffing, restart
    markers, and EOI. It deliberately does not claim to prove that every entropy-coded block is
    mathematically valid; a decoder dependency would be needed for that final step.
    """
    if len(body) < 4 or body[:2] != b"\xff\xd8":
        return False

    offset = 2
    frame_components: dict[int, int] = {}
    frame_marker: int | None = None
    quantization_tables: set[int] = set()
    huffman_tables: set[tuple[int, int]] = set()
    saw_scan = False
    saw_eoi = False

    while offset < len(body):
        if body[offset] != 0xFF:
            return False
        while offset < len(body) and body[offset] == 0xFF:
            offset += 1
        if offset >= len(body):
            return False
        marker = body[offset]
        offset += 1
        if marker == 0xD9:
            saw_eoi = True
            break
        if marker in {0x00, 0xD8} or 0xD0 <= marker <= 0xD7:
            return False
        if offset + 2 > len(body):
            return False
        segment_length = struct.unpack_from(">H", body, offset)[0]
        if segment_length < 2 or offset + segment_length > len(body):
            return False
        segment = body[offset + 2 : offset + segment_length]
        offset += segment_length

        if marker in _JPEG_SOF_MARKERS:
            parsed = _jpeg_frame(segment)
            if parsed is None or frame_components:
                return False
            width, height, components = parsed
            if width * height > _MAX_IMAGE_PIXELS:
                return False
            frame_components = components
            frame_marker = marker
        elif marker == 0xDB:
            parsed_tables = _jpeg_quantization_tables(segment)
            if parsed_tables is None:
                return False
            quantization_tables.update(parsed_tables)
        elif marker == 0xC4:
            parsed_huffman_tables = _jpeg_huffman_tables(segment)
            if parsed_huffman_tables is None:
                return False
            huffman_tables.update(parsed_huffman_tables)
        elif marker == 0xDA:
            if not frame_components or frame_marker is None:
                return False
            scan = _jpeg_scan(segment, frame_components, quantization_tables, huffman_tables)
            if scan is None:
                return False
            spectral_start, spectral_end, successive = scan
            progressive = frame_marker in {0xC2, 0xC6, 0xCA, 0xCE}
            if not progressive and (spectral_start, spectral_end, successive) != (0, 63, 0):
                return False
            if progressive and not (0 <= spectral_start <= spectral_end <= 63):
                return False
            saw_scan = True
            entropy_end = _jpeg_entropy_end(body, offset)
            if entropy_end is None:
                return False
            offset = entropy_end
            if offset < len(body) and body[offset : offset + 2] == b"\xff\xd9":
                offset += 2
                saw_eoi = True
                break
        elif marker == 0xDD and len(segment) != 2:
            return False

    return saw_scan and saw_eoi and bool(frame_components) and offset == len(body)


def _jpeg_frame(segment: bytes) -> tuple[int, int, dict[int, int]] | None:
    if len(segment) < 6:
        return None
    precision = segment[0]
    height, width, component_count = struct.unpack_from(">HHB", segment, 1)
    if (
        precision not in {8, 12}
        or width == 0
        or height == 0
        or component_count == 0
        or len(segment) != 6 + component_count * 3
    ):
        return None
    components: dict[int, int] = {}
    offset = 6
    for _ in range(component_count):
        component_id = segment[offset]
        sampling = segment[offset + 1]
        quantization_table = segment[offset + 2]
        if component_id in components or sampling == 0 or quantization_table > 3:
            return None
        components[component_id] = quantization_table
        offset += 3
    return width, height, components


def _jpeg_quantization_tables(segment: bytes) -> set[int] | None:
    tables: set[int] = set()
    offset = 0
    while offset < len(segment):
        info = segment[offset]
        offset += 1
        precision = info >> 4
        table_id = info & 0x0F
        if precision not in {0, 1} or table_id > 3:
            return None
        table_bytes = 64 * (2 if precision else 1)
        if offset + table_bytes > len(segment):
            return None
        tables.add(table_id)
        offset += table_bytes
    return tables if offset == len(segment) else None


def _jpeg_huffman_tables(segment: bytes) -> set[tuple[int, int]] | None:
    tables: set[tuple[int, int]] = set()
    offset = 0
    while offset < len(segment):
        info = segment[offset]
        offset += 1
        table_class = info >> 4
        table_id = info & 0x0F
        if table_class not in {0, 1} or table_id > 3 or offset + 16 > len(segment):
            return None
        code_counts = segment[offset : offset + 16]
        offset += 16
        value_count = sum(code_counts)
        if value_count > 162 or offset + value_count > len(segment):
            return None
        offset += value_count
        tables.add((table_class, table_id))
    return tables if offset == len(segment) else None


def _jpeg_scan(
    segment: bytes,
    frame_components: dict[int, int],
    quantization_tables: set[int],
    huffman_tables: set[tuple[int, int]],
) -> tuple[int, int, int] | None:
    if len(segment) < 4:
        return None
    component_count = segment[0]
    expected_length = 1 + component_count * 2 + 3
    if component_count == 0 or len(segment) != expected_length:
        return None
    offset = 1
    for _ in range(component_count):
        component_id = segment[offset]
        selectors = segment[offset + 1]
        if component_id not in frame_components:
            return None
        dc_table = selectors >> 4
        ac_table = selectors & 0x0F
        if (0, dc_table) not in huffman_tables or (1, ac_table) not in huffman_tables:
            return None
        if frame_components[component_id] not in quantization_tables:
            return None
        offset += 2
    spectral_start, spectral_end, successive = struct.unpack_from(">BBB", segment, offset)
    if successive >> 4 > 13 or (successive & 0x0F) > 13:
        return None
    return spectral_start, spectral_end, successive


def _jpeg_entropy_end(body: bytes, offset: int) -> int | None:
    while offset < len(body):
        if body[offset] != 0xFF:
            offset += 1
            continue
        marker_offset = offset
        while offset < len(body) and body[offset] == 0xFF:
            offset += 1
        if offset >= len(body):
            return None
        marker = body[offset]
        if marker == 0x00 or 0xD0 <= marker <= 0xD7:
            offset += 1
            continue
        if marker == 0xD9:
            return marker_offset
        return marker_offset
    return None


def _decode_webp(body: bytes) -> bool:
    if len(body) < 12 or body[:4] != b"RIFF" or body[8:12] != b"WEBP":
        return False
    riff_size = struct.unpack_from("<I", body, 4)[0]
    if riff_size != len(body) - 8:
        return False

    offset = 12
    image_chunks = 0
    canvas: tuple[int, int] | None = None
    while offset < len(body):
        if offset + 8 > len(body):
            return False
        chunk_type = body[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", body, offset + 4)[0]
        data_start = offset + 8
        data_end = data_start + chunk_size
        padded_end = data_end + (chunk_size & 1)
        if data_end > len(body) or padded_end > len(body):
            return False
        chunk = body[data_start:data_end]

        if chunk_type == b"VP8 ":
            dimensions = _webp_vp8_dimensions(chunk)
            if dimensions is None:
                return False
            image_chunks += 1
            canvas = dimensions if canvas is None else canvas
        elif chunk_type == b"VP8L":
            dimensions = _webp_vp8l_dimensions(chunk)
            if dimensions is None:
                return False
            image_chunks += 1
            canvas = dimensions if canvas is None else canvas
        elif chunk_type == b"VP8X":
            dimensions = _webp_vp8x_dimensions(chunk)
            if dimensions is None:
                return False
            canvas = dimensions
        elif chunk_type == b"ANIM":
            if len(chunk) != 6:
                return False
        elif chunk_type == b"ANMF":
            if len(chunk) < 16:
                return False
            frame_payload = chunk[16:]
            if not _decode_webp_frame_payload(frame_payload):
                return False
            image_chunks += 1
        offset = padded_end

    return offset == len(body) and image_chunks > 0 and canvas is not None


def _webp_vp8_dimensions(chunk: bytes) -> tuple[int, int] | None:
    if len(chunk) < 10 or chunk[3:6] != b"\x9d\x01\x2a":
        return None
    width, height = struct.unpack_from("<HH", chunk, 6)
    width &= 0x3FFF
    height &= 0x3FFF
    return _image_dimensions(width, height)


def _webp_vp8l_dimensions(chunk: bytes) -> tuple[int, int] | None:
    if len(chunk) < 5 or chunk[0] != 0x2F:
        return None
    header = int.from_bytes(chunk[1:5], "little")
    if (header >> 29) & 0x07:
        return None
    width = (header & 0x3FFF) + 1
    height = ((header >> 14) & 0x3FFF) + 1
    return _image_dimensions(width, height)


def _webp_vp8x_dimensions(chunk: bytes) -> tuple[int, int] | None:
    if len(chunk) != 10 or chunk[0] & 0xC0:
        return None
    width = int.from_bytes(chunk[4:7], "little") + 1
    height = int.from_bytes(chunk[7:10], "little") + 1
    return _image_dimensions(width, height)


def _decode_webp_frame_payload(payload: bytes) -> bool:
    offset = 0
    found = False
    while offset < len(payload):
        if offset + 8 > len(payload):
            return False
        chunk_type = payload[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", payload, offset + 4)[0]
        data_start = offset + 8
        data_end = data_start + chunk_size
        padded_end = data_end + (chunk_size & 1)
        if data_end > len(payload) or padded_end > len(payload):
            return False
        chunk = payload[data_start:data_end]
        if chunk_type == b"VP8 ":
            found = _webp_vp8_dimensions(chunk) is not None
        elif chunk_type == b"VP8L":
            found = _webp_vp8l_dimensions(chunk) is not None
        offset = padded_end
    return found and offset == len(payload)


def _image_dimensions(width: int, height: int) -> tuple[int, int] | None:
    if width == 0 or height == 0 or width * height > _MAX_IMAGE_PIXELS:
        return None
    return width, height


def _decode_gif(body: bytes) -> bool:
    if len(body) < 13 or body[:6] not in {b"GIF87a", b"GIF89a"}:
        return False
    width, height = struct.unpack_from("<HH", body, 6)
    if width == 0 or height == 0 or width * height > _MAX_IMAGE_PIXELS:
        return False

    offset = 13
    packed = body[10]
    if packed & 0x80:
        offset += 3 * (1 << ((packed & 0x07) + 1))
    if offset > len(body):
        return False

    frames = 0
    while offset < len(body):
        introducer = body[offset]
        offset += 1
        if introducer == 0x3B:
            return frames > 0 and offset == len(body)
        if introducer == 0x21:
            if offset >= len(body):
                return False
            offset += 1
            subblocks_end = _skip_gif_subblocks(body, offset)
            if subblocks_end is None:
                return False
            offset = subblocks_end
            continue
        if introducer != 0x2C or offset + 9 > len(body):
            return False

        left, top, frame_width, frame_height = struct.unpack_from("<HHHH", body, offset)
        descriptor_packed = body[offset + 8]
        offset += 9
        if (
            frame_width == 0
            or frame_height == 0
            or left + frame_width > width
            or top + frame_height > height
            or frame_width * frame_height > _MAX_IMAGE_PIXELS
        ):
            return False
        if descriptor_packed & 0x80:
            offset += 3 * (1 << ((descriptor_packed & 0x07) + 1))
            if offset > len(body):
                return False
        if offset >= len(body):
            return False
        minimum_code_size = body[offset]
        offset += 1
        if not 2 <= minimum_code_size <= 8:
            return False
        compressed, offset = _read_gif_subblocks(body, offset)
        if compressed is None or not _decode_gif_lzw(
            compressed, minimum_code_size, frame_width * frame_height
        ):
            return False
        frames += 1

    return False


def _skip_gif_subblocks(body: bytes, offset: int) -> int | None:
    while True:
        if offset >= len(body):
            return None
        size = body[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
        if offset > len(body):
            return None


def _read_gif_subblocks(body: bytes, offset: int) -> tuple[bytes | None, int]:
    blocks = bytearray()
    while True:
        if offset >= len(body):
            return None, offset
        size = body[offset]
        offset += 1
        if size == 0:
            return bytes(blocks), offset
        if offset + size > len(body) or len(blocks) + size > MAX_IMAGE_BYTES:
            return None, offset
        blocks.extend(body[offset : offset + size])
        offset += size


def _decode_gif_lzw(compressed: bytes, minimum_code_size: int, expected_pixels: int) -> bool:
    clear_code = 1 << minimum_code_size
    end_code = clear_code + 1
    code_size = minimum_code_size + 1
    next_code = end_code + 1
    table = {code: bytes((code,)) for code in range(clear_code)}
    bit_position = 0
    previous: bytes | None = None
    pixels = 0
    saw_end = False

    while bit_position + code_size <= len(compressed) * 8:
        code = 0
        for bit in range(code_size):
            code |= (
                (compressed[(bit_position + bit) // 8] >> ((bit_position + bit) % 8)) & 1
            ) << bit
        bit_position += code_size
        if code == clear_code:
            table = {entry: bytes((entry,)) for entry in range(clear_code)}
            code_size = minimum_code_size + 1
            next_code = end_code + 1
            previous = None
            continue
        if code == end_code:
            saw_end = True
            break

        if previous is None:
            entry = table.get(code)
            if entry is None:
                return False
        elif code in table:
            entry = table[code]
        elif code == next_code:
            entry = previous + previous[:1]
        else:
            return False

        pixels += len(entry)
        if pixels > expected_pixels:
            return False
        if previous is not None and next_code < 4096:
            table[next_code] = previous + entry[:1]
            next_code += 1
            if next_code == 1 << code_size and code_size < 12:
                code_size += 1
        previous = entry

    return saw_end and pixels == expected_pixels


def _decode_bmp(body: bytes) -> bool:
    if len(body) < 26 or body[:2] != b"BM":
        return False
    pixel_offset = struct.unpack_from("<I", body, 10)[0]
    header_size = struct.unpack_from("<I", body, 14)[0]
    if header_size == 12:
        if len(body) < 26:
            return False
        width, height, planes, bits_per_pixel = struct.unpack_from("<HHHH", body, 18)
        compression = 0
        colors_used = 0
        palette_entry_size = 3
    elif header_size >= 40:
        if len(body) < 14 + header_size:
            return False
        width, signed_height, planes, bits_per_pixel, compression = struct.unpack_from(
            "<iiHHI", body, 18
        )
        colors_used = struct.unpack_from("<I", body, 46)[0]
        height = abs(signed_height)
        palette_entry_size = 4
    else:
        return False

    if (
        width <= 0
        or height <= 0
        or width * height > _MAX_IMAGE_PIXELS
        or planes != 1
        or bits_per_pixel not in {1, 4, 8, 16, 24, 32}
        or compression != 0
    ):
        return False
    row_bytes = ((width * bits_per_pixel + 31) // 32) * 4
    pixel_bytes = row_bytes * height
    if pixel_bytes > _MAX_DECODED_IMAGE_BYTES or pixel_offset > len(body):
        return False
    if bits_per_pixel <= 8:
        palette_entries = colors_used or (1 << bits_per_pixel)
        palette_end = 14 + header_size + palette_entries * palette_entry_size
        if palette_end > len(body) or pixel_offset < palette_end:
            return False
    return bool(pixel_offset + pixel_bytes <= len(body))


_TIFF_TYPE_SIZES = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
}


def _decode_tiff(body: bytes) -> bool:
    if len(body) < 8 or body[:2] not in {b"II", b"MM"}:
        return False
    endian = "<" if body[:2] == b"II" else ">"
    if struct.unpack_from(f"{endian}H", body, 2)[0] != 42:
        return False
    first_ifd = struct.unpack_from(f"{endian}I", body, 4)[0]
    if first_ifd == 0:
        return False

    offset = first_ifd
    visited: set[int] = set()
    first_entries: dict[int, tuple[int, ...] | None] | None = None
    for _ in range(64):
        if offset == 0:
            break
        if offset in visited:
            return False
        visited.add(offset)
        parsed = _tiff_ifd(body, offset, endian)
        if parsed is None:
            return False
        entries, offset = parsed
        if first_entries is None:
            first_entries = entries
    else:
        return False

    if not first_entries:
        return False
    width = _tiff_scalar(first_entries, 256)
    height = _tiff_scalar(first_entries, 257)
    compression = _tiff_scalar(first_entries, 259)
    strip_offsets = first_entries.get(273)
    strip_byte_counts = first_entries.get(279)
    if (
        width is None
        or height is None
        or width <= 0
        or height <= 0
        or width * height > _MAX_IMAGE_PIXELS
        or compression not in {1, 5, 7, 8, 32773, 32946}
        or strip_offsets is None
        or strip_byte_counts is None
        or len(strip_offsets) == 0
        or len(strip_offsets) != len(strip_byte_counts)
    ):
        return False
    for strip_offset, byte_count in zip(strip_offsets, strip_byte_counts, strict=True):
        if (
            strip_offset < 0
            or byte_count <= 0
            or strip_offset > len(body)
            or byte_count > len(body) - strip_offset
        ):
            return False
    rows_per_strip = _tiff_scalar(first_entries, 278)
    return rows_per_strip is None or rows_per_strip > 0


def _tiff_ifd(
    body: bytes, offset: int, endian: str
) -> tuple[dict[int, tuple[int, ...] | None], int] | None:
    if offset + 2 > len(body):
        return None
    count = struct.unpack_from(f"{endian}H", body, offset)[0]
    if count > 4096:
        return None
    entries_end = offset + 2 + count * 12 + 4
    if entries_end > len(body):
        return None
    entries: dict[int, tuple[int, ...] | None] = {}
    entry_offset = offset + 2
    for _ in range(count):
        tag, value_type, value_count = struct.unpack_from(f"{endian}HHI", body, entry_offset)
        raw_value = body[entry_offset + 8 : entry_offset + 12]
        entry_offset += 12
        value_size = _TIFF_TYPE_SIZES.get(value_type)
        if value_size is None or value_count == 0:
            return None
        total_size = value_size * value_count
        if total_size <= 4:
            value_bytes = raw_value[:total_size]
        else:
            value_offset = struct.unpack_from(f"{endian}I", raw_value, 0)[0]
            if value_offset > len(body) or total_size > len(body) - value_offset:
                return None
            value_bytes = body[value_offset : value_offset + total_size]
        values = _tiff_values(value_type, value_count, value_bytes, endian)
        if values is None and value_type not in {2, 5, 7, 10, 11, 12}:
            return None
        entries[tag] = values
    next_ifd = struct.unpack_from(f"{endian}I", body, entry_offset)[0]
    return entries, next_ifd


def _tiff_values(value_type: int, count: int, body: bytes, endian: str) -> tuple[int, ...] | None:
    formats = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i"}
    element_format = formats.get(value_type)
    if element_format is None:
        return None
    return struct.unpack(f"{endian}{count}{element_format}", body)


def _tiff_scalar(entries: dict[int, tuple[int, ...] | None], tag: int) -> int | None:
    values = entries.get(tag)
    if values is None or len(values) != 1:
        return None
    return values[0]


def _decode_ico(body: bytes) -> bool:
    if len(body) < 6 or struct.unpack_from("<HH", body, 0) != (0, 1):
        return False
    image_count = struct.unpack_from("<H", body, 4)[0]
    if image_count == 0 or image_count > 1024 or 6 + image_count * 16 > len(body):
        return False
    for index in range(image_count):
        entry = 6 + index * 16
        image_size, image_offset = struct.unpack_from("<II", body, entry + 8)
        image_end = image_offset + image_size
        if image_size == 0 or image_end > len(body):
            continue
        embedded = body[image_offset:image_end]
        if embedded.startswith(b"\x89PNG\r\n\x1a\n") and _decode_png(embedded):
            return True
        if _decode_ico_bitmap(embedded):
            return True
    return False


def _decode_ico_bitmap(body: bytes) -> bool:
    if len(body) < 40:
        return False
    header_size = struct.unpack_from("<I", body, 0)[0]
    if header_size < 40 or header_size > len(body):
        return False
    width, total_height, planes, bits_per_pixel, compression = struct.unpack_from("<iiHHI", body, 4)
    if (
        width <= 0
        or total_height <= 0
        or total_height % 2
        or planes != 1
        or bits_per_pixel not in {1, 4, 8, 16, 24, 32}
        or compression != 0
    ):
        return False
    height = total_height // 2
    if width * height > _MAX_IMAGE_PIXELS:
        return False
    colors_used = struct.unpack_from("<I", body, 32)[0] if header_size >= 40 else 0
    palette_entries = colors_used or (1 << bits_per_pixel if bits_per_pixel <= 8 else 0)
    pixel_offset = header_size + palette_entries * 4
    xor_row_bytes = ((width * bits_per_pixel + 31) // 32) * 4
    and_row_bytes = ((width + 31) // 32) * 4
    required = xor_row_bytes * height + and_row_bytes * height
    return bool(pixel_offset <= len(body) and required <= len(body) - pixel_offset)


def _decode_jp2(body: bytes) -> bool:
    signature = b"\x00\x00\x00\x0cjP  \r\n\x87\n"
    if not body.startswith(signature):
        return False
    boxes = _read_jp2_boxes(body, len(signature))
    if boxes is None:
        return False
    saw_header = False
    saw_codestream = False
    for box_type, payload in boxes:
        if box_type == b"jp2h":
            nested = _read_jp2_boxes(payload, 0)
            if nested is None:
                return False
            for nested_type, nested_payload in nested:
                if nested_type == b"ihdr" and len(nested_payload) >= 14:
                    height, width = struct.unpack_from(">II", nested_payload, 0)
                    if _image_dimensions(width, height) is None:
                        return False
                    saw_header = True
        elif box_type == b"jp2c" and payload:
            saw_codestream = True
    return saw_header and saw_codestream


def _read_jp2_boxes(body: bytes, offset: int) -> list[tuple[bytes, bytes]] | None:
    boxes: list[tuple[bytes, bytes]] = []
    while offset < len(body):
        if offset + 8 > len(body):
            return None
        length, box_type = struct.unpack_from(">I4s", body, offset)
        header_size = 8
        if length == 1:
            if offset + 16 > len(body):
                return None
            length = struct.unpack_from(">Q", body, offset + 8)[0]
            header_size = 16
        elif length == 0:
            length = len(body) - offset
        if length < header_size or length > len(body) - offset:
            return None
        payload_start = offset + header_size
        boxes.append((box_type, body[payload_start : offset + length]))
        offset += length
    return boxes


def _decode_isobmff_image(body: bytes, content_type: str) -> bool:
    boxes = _read_isobmff_boxes(body)
    if boxes is None:
        return False
    brands = {
        "avif": {b"avif", b"avis"},
        "heic": {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"},
    }[content_type.removeprefix("image/")]
    ftyp = next((payload for box_type, payload in boxes if box_type == b"ftyp"), None)
    if ftyp is None or len(ftyp) < 8:
        return False
    declared_brands = {ftyp[:4], *[ftyp[index : index + 4] for index in range(8, len(ftyp), 4)]}
    if not declared_brands & brands:
        return False
    meta = next((payload for box_type, payload in boxes if box_type == b"meta"), None)
    mdat = next((payload for box_type, payload in boxes if box_type == b"mdat"), None)
    if meta is None or len(meta) < 4 or not mdat:
        return False
    nested = _read_isobmff_boxes(meta, 4)
    if nested is None:
        return False
    dimensions: tuple[int, int] | None = None
    for box_type, payload in nested:
        if box_type == b"iprp":
            properties = _read_isobmff_boxes(payload)
            if properties is None:
                return False
            for property_type, property_payload in properties:
                if property_type != b"ipco":
                    continue
                image_properties = _read_isobmff_boxes(property_payload)
                if image_properties is None:
                    return False
                for image_property, image_payload in image_properties:
                    if image_property == b"ispe" and len(image_payload) >= 12:
                        width, height = struct.unpack_from(">II", image_payload, 4)
                        dimensions = _image_dimensions(width, height)
                        if dimensions is None:
                            return False
    return dimensions is not None


def _read_isobmff_boxes(body: bytes, offset: int = 0) -> list[tuple[bytes, bytes]] | None:
    boxes: list[tuple[bytes, bytes]] = []
    while offset < len(body):
        if offset + 8 > len(body):
            return None
        size, box_type = struct.unpack_from(">I4s", body, offset)
        header_size = 8
        if size == 1:
            if offset + 16 > len(body):
                return None
            size = struct.unpack_from(">Q", body, offset + 8)[0]
            header_size = 16
        elif size == 0:
            size = len(body) - offset
        if size < header_size or size > len(body) - offset:
            return None
        payload_start = offset + header_size
        boxes.append((box_type, body[payload_start : offset + size]))
        offset += size
    return boxes


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
