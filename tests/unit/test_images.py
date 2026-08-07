"""Image cache and public-route security regressions."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from aggregato.api.routes.images import _PLACEHOLDER, image
from aggregato.images import cache as image_cache
from aggregato.images.cache import cached_image, url_hash

_PNG = b"\x89PNG\r\n\x1a\nfixture"
_PNG_HASH = sha256(_PNG).hexdigest()


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
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


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
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
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
    calls: list[httpx.Request] = []
    _mock_client(monkeypatch, lambda request: calls.append(request) or httpx.Response(200))
    digest = await _register(engine, "https://image.example/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert calls == []


async def test_redirect_target_is_validated_before_following(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            302,
            request=request,
            headers={"location": "https://127.0.0.1/private.png"},
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert [str(request.url) for request in calls] == ["https://93.184.216.34/image.png"]


async def test_dns_is_rechecked_for_the_connection_request(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = iter(("93.184.216.34", "127.0.0.1"))

    def rebinding_result(
        host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del host, args, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(results), port))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_result)
    calls: list[httpx.Request] = []
    _mock_client(monkeypatch, lambda request: calls.append(request) or httpx.Response(200))
    digest = await _register(engine, "https://image.example/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert calls == []


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
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, request=request, headers={"content-type": content_type}, content=body
        )

    _mock_client(monkeypatch, respond)
    digest = await _register(engine, "https://93.184.216.34/image.png")

    assert await cached_image(engine, data_dir, digest) is None
    assert list((data_dir / "images").rglob("*.tmp")) == []


async def test_response_size_is_bounded_before_publication(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "MAX_IMAGE_BYTES", 16)
    body = _PNG + b"x" * 16

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
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

    class _Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            nonlocal streamed
            streamed = True
            yield _PNG

        async def aclose(self) -> None:
            return None

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
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

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
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

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
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

    async def fake_fetch(source_url: str, root: Path) -> tuple[Path, str, str, int]:
        nonlocal calls
        del source_url
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


class _InterruptedStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> Any:
        yield _PNG
        raise OSError("connection interrupted")

    async def aclose(self) -> None:
        return None


async def test_interrupted_stream_leaves_no_partial_file(
    engine: _FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
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
    ) -> tuple[Path, str]:
        assert enabled
        assert now is not None
        return path, "image/png"

    monkeypatch.setattr("aggregato.api.routes.images.get_settings", settings)
    monkeypatch.setattr("aggregato.api.routes.images.cached_image", found)

    response = await image(request, "a" * 64)

    assert response.headers["cache-control"] == "public, max-age=86400"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.headers["content-disposition"].startswith("attachment")
