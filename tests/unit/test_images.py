"""Image cache and public-route security regressions."""

from __future__ import annotations

import asyncio
import base64
import socket
import struct
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

from aggregato.api.routes.images import _PLACEHOLDER, _add_base_url_origin, image
from aggregato.images import cache as image_cache
from aggregato.images.cache import cached_image, image_origin, url_hash

_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PNG_HASH = sha256(_PNG).hexdigest()
_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAVAQEBAAAAAAAAAAAAAAAAAAAHCf/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/ADoDFU3/2Q=="
)
_PROGRESSIVE_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wgARCAABAAEDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAAB//EABUBAQEAAAAAAAAAAAAAAAAAAAYI/9oADAMBAAIQAxAAAAE5C1T/AP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAQUCf//EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQMBAT8Bf//EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQIBAT8Bf//EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEABj8Cf//EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAT8hf//aAAwDAQACAAMAAAAQ/wD/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/EH//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/EH//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/EH//2Q=="
)
_GIF = base64.b64decode("R0lGODlhAQABAPAAAP8AAAAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==")
_WEBP = base64.b64decode(
    "UklGRjwAAABXRUJQVlA4IDAAAADQAQCdASoBAAEAAgA0JaACdLoB+AADsAD+8MQL/yC5YXXI1/8gP+QH/ID/+PIAAAA="
)
_BMP = (
    b"BM"
    + struct.pack("<IHHI", 58, 0, 0, 54)
    + struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0)
    + b"\x00\x00\xff\x00"
)
_ICO = (
    struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 1, 1, 0, 0, 1, 32, len(_PNG), 22) + _PNG
)


def _make_tiff() -> bytes:
    def entry(tag: int, value_type: int, value: int) -> bytes:
        if value_type == 3:
            raw = struct.pack("<H", value) + b"\x00\x00"
        else:
            raw = struct.pack("<I", value)
        return struct.pack("<HHI", tag, value_type, 1) + raw

    pixel_offset = 8 + 2 + 9 * 12 + 4
    entries = b"".join(
        [
            entry(256, 3, 1),
            entry(257, 3, 1),
            entry(258, 3, 8),
            entry(259, 3, 1),
            entry(262, 3, 1),
            entry(273, 4, pixel_offset),
            entry(277, 3, 1),
            entry(278, 4, 1),
            entry(279, 4, 1),
        ]
    )
    return (
        b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 9) + entries + b"\x00" * 4 + b"\x80"
    )


_TIFF = _make_tiff()


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, box_type) + payload


_JP2 = (
    b"\x00\x00\x00\x0cjP  \r\n\x87\n"
    + _box(b"jp2h", _box(b"ihdr", struct.pack(">IIHBBBB", 1, 1, 3, 7, 7, 0, 0)))
    + _box(b"jp2c", b"\xff\x4f")
)


def _make_isobmff(brand: bytes) -> bytes:
    image_property = _box(b"ispe", b"\x00\x00\x00\x00" + struct.pack(">II", 1, 1))
    properties = _box(b"ipco", image_property)
    meta = _box(b"meta", b"\x00\x00\x00\x00" + _box(b"iprp", properties))
    return _box(b"ftyp", brand + b"\x00\x00\x00\x00" + brand) + meta + _box(b"mdat", b"\x00")


_AVIF = _make_isobmff(b"avif")
_HEIC = _make_isobmff(b"heic")


class _FakeResult:
    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row

    def first(self) -> _FakeRow | None:
        return self._row


class _FakeRow:
    def __init__(self, source_url: str) -> None:
        self.url_hash = url_hash(source_url)
        self.source_url = source_url
        self.bytes_sha256: str | None = None
        self.content_type: str | None = None
        self.size_bytes: int | None = None
        self.failure_count = 0


class _FakeDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, _FakeRow] = {}

    async def execute(self, statement: Any) -> _FakeResult:
        row = next(iter(self.rows.values()), None)
        if statement.is_update and row is not None:
            for name, value in statement._values.items():
                current = getattr(value, "value", value)
                if name == "failure_count":
                    row.failure_count += 1
                elif hasattr(row, name):
                    setattr(row, name, current)
        return _FakeResult(row if statement.is_select else None)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _FakeDatabase:
    database = _FakeDatabase()

    @asynccontextmanager
    async def transaction(_engine: object) -> Any:
        yield database

    monkeypatch.setattr(image_cache, "transaction", transaction)
    return database


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx2.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx2.AsyncClient:
        return real_client(*args, transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", factory)


async def _register(engine: _FakeDatabase, url: str) -> str:
    digest = url_hash(url)
    engine.rows[digest] = _FakeRow(url)
    return digest


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/image.png",
        "https://127.0.0.1/image.png",
        "https://10.0.0.1/image.png",
        "https://169.254.169.254/image.png",
        "https://224.0.0.1/image.png",
        "https://0.0.0.0/image.png",
        "https://[::1]/image.png",
        "https://[ff02::1]/image.png",
        "https://[fe80::1]/image.png",
        "https://[::]/image.png",
    ],
)
async def test_image_source_rejects_unsafe_urls(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    calls: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            200, request=request, headers={"content-type": "image/png"}, content=_PNG
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, url)

    assert await cached_image(engine, data_dir, digest) is None
    assert calls == []


async def test_dns_results_are_checked_before_a_request(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def private_result(
        host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del host, args, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]

    monkeypatch.setattr(socket, "getaddrinfo", private_result)
    calls: list[httpx2.Request] = []
    _mock_client(monkeypatch, lambda request: calls.append(request) or httpx2.Response(200))
    digest = await _register(engine, "https://image.example/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert calls == []


async def test_explicitly_configured_private_origin_can_serve_artwork(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def private_result(
        host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del host, args, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.23.0.2", port))]

    monkeypatch.setattr(socket, "getaddrinfo", private_result)
    calls: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            200, request=request, headers={"content-type": "image/webp"}, content=_WEBP
        )

    _mock_client(monkeypatch, respond)
    source_url = "http://koito:4110/image/track.webp"
    digest = await _register(engine, source_url)

    result = await cached_image(
        engine,
        data_dir,
        digest,
        allowed_origins={image_origin(source_url)},
    )

    assert result is not None
    assert result[1] == "image/webp"
    assert calls[0].url.host == "172.23.0.2"
    assert calls[0].headers["host"] == "koito:4110"


def test_only_a_provider_base_url_becomes_a_private_image_origin() -> None:
    origins: set[str] = set()

    _add_base_url_origin({"base_url": "http://koito:4110/apis"}, origins)
    _add_base_url_origin({"base_url": "http://user:secret@koito:4110"}, origins)

    assert origins == {"http://koito:4110"}


async def test_redirect_target_is_validated_before_following(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            302,
            request=request,
            headers={"location": "https://127.0.0.1/private.png"},
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert [str(request.url) for request in calls] == ["https://93.184.216.34/image.png"]


async def test_dns_address_is_pinned_for_the_connection_request(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls_to_resolver = 0

    def rebinding_result(
        host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        nonlocal calls_to_resolver
        del host, args, kwargs
        calls_to_resolver += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_result)
    calls: list[httpx2.Request] = []
    _mock_client(monkeypatch, lambda request: calls.append(request) or httpx2.Response(200))
    digest = await _register(engine, "https://image.example/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert calls_to_resolver == 1
    assert [str(request.url) for request in calls] == ["https://93.184.216.34/image.png"]
    assert calls[0].headers["host"] == "image.example"
    assert calls[0].extensions["sni_hostname"] == "image.example"


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
        ("image/png", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
        ("image/png", b"not an image"),
    ],
)
async def test_image_body_must_be_a_non_svg_known_format(
    engine: _FakeDatabase,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    body: bytes,
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, request=request, headers={"content-type": content_type}, content=body
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert list((data_dir / "images").rglob("*.tmp")) == []


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("image/png", _PNG),
        ("image/jpeg", _JPEG),
        ("image/jpeg", _PROGRESSIVE_JPEG),
        ("image/gif", _GIF),
        ("image/webp", _WEBP),
        ("image/bmp", _BMP),
        ("image/tiff", _TIFF),
        ("image/x-icon", _ICO),
        ("image/jp2", _JP2),
        ("image/avif", _AVIF),
        ("image/heic", _HEIC),
    ],
)
async def test_common_raster_formats_pass_magic_detection(
    engine: _FakeDatabase,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    body: bytes,
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, request=request, headers={"content-type": content_type}, content=body
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")

    result = await cached_image(engine, data_dir, digest)

    assert result is not None
    assert result[1] == content_type
    assert result[0].read_bytes() == body


async def test_response_size_is_bounded_before_publication(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "MAX_IMAGE_BYTES", 16)
    body = _PNG + b"x" * 16

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, request=request, headers={"content-type": "image/png"}, content=body
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert list((data_dir / "images").rglob("*.tmp")) == []
    assert list((data_dir / "images").rglob(_PNG_HASH)) == []


async def test_declared_response_size_is_bounded_before_streaming(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "MAX_IMAGE_BYTES", 16)
    streamed = False

    class _Body(httpx2.AsyncByteStream):
        async def __aiter__(self) -> Any:
            nonlocal streamed
            streamed = True
            yield _PNG

        async def aclose(self) -> None:
            return None

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            request=request,
            headers={"content-type": "image/png", "content-length": "17"},
            stream=_Body(),
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert not streamed
    assert list((data_dir / "images").rglob("*.tmp")) == []


async def test_valid_image_is_hashed_and_published_atomically(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(
            200, request=request, headers={"content-type": "image/jpeg"}, content=_PNG
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")

    first = await cached_image(engine, data_dir, digest)
    second = await cached_image(engine, data_dir, digest)

    assert first == (data_dir / "images" / _PNG_HASH[:2] / _PNG_HASH, "image/png")
    assert second == first
    assert first[0].read_bytes() == _PNG
    assert calls == 1
    assert list((data_dir / "images").rglob("*.tmp")) == []


async def test_corrupt_cached_bytes_are_not_served(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(
            200, request=request, headers={"content-type": "image/png"}, content=_PNG
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")
    first = await cached_image(engine, data_dir, digest)
    assert first is not None
    first[0].write_bytes(b"corrupt")

    second = await cached_image(engine, data_dir, digest)

    assert second == first
    assert second[0].read_bytes() == _PNG
    assert calls == 2


async def test_concurrent_misses_share_one_digest_fetch(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    path = data_dir / "images" / _PNG_HASH[:2] / _PNG_HASH

    async def fake_fetch(
        source_url: str,
        root: Path,
        *,
        allowed_origins: object,
    ) -> tuple[Path, str, str, int]:
        nonlocal calls
        del source_url, allowed_origins
        assert root == data_dir
        calls += 1
        entered.set()
        await release.wait()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_PNG)
        return path, "image/png", _PNG_HASH, len(_PNG)

    monkeypatch.setattr(image_cache, "_fetch_and_store", fake_fetch)
    digest = await _register(engine, "https://93.184.216.34/image.png")
    first = asyncio.create_task(cached_image(engine, data_dir, digest))
    await entered.wait()
    second = asyncio.create_task(cached_image(engine, data_dir, digest))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()

    assert await asyncio.gather(first, second) == [
        (path, "image/png"),
        (path, "image/png"),
    ]
    assert calls == 1


class _InterruptedStream(httpx2.AsyncByteStream):
    async def __aiter__(self) -> Any:
        yield _PNG
        raise OSError("connection interrupted")

    async def aclose(self) -> None:
        return None


async def test_interrupted_stream_leaves_no_partial_file(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            request=request,
            headers={"content-type": "image/png"},
            stream=_InterruptedStream(),
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert list((data_dir / "images").rglob("*.tmp")) == []
    assert list((data_dir / "images").rglob(_PNG_HASH)) == []


async def test_public_route_rejects_non_digest_paths_without_cache_access() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(image_cache_enabled=True),
                engine=object(),
            )
        )
    )

    response = await image(request, "not-a-source")

    assert response.media_type == "image/gif"
    assert response.body == _PLACEHOLDER
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment")


async def test_public_route_sets_security_headers_on_cached_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(_PNG)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(image_cache_enabled=True, data_dir=tmp_path),
                engine=object(),
            )
        )
    )

    async def settings(_engine: object) -> dict[str, bool]:
        return {"image_cache_enabled": True}

    async def found(
        _engine: object,
        _data_dir: Path,
        _digest: str,
        *,
        enabled: bool,
        now: datetime | None,
        allowed_origins: frozenset[str],
    ) -> tuple[Path, str]:
        assert enabled
        assert now is not None
        assert not allowed_origins
        return path, "image/png"

    async def no_configured_origins(_request: Any) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(
        "aggregato.api.routes.images._configured_image_origins", no_configured_origins
    )
    monkeypatch.setattr("aggregato.api.routes.images.get_settings", settings)
    monkeypatch.setattr("aggregato.api.routes.images.cached_image", found)

    response = await image(request, "a" * 64)

    assert response.headers["cache-control"] == "public, max-age=86400"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.headers["content-disposition"].startswith("attachment")
