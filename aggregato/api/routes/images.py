"""Serve local cached images; no third-party redirect ever reaches the browser (T078)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from starlette.requests import Request

from aggregato.images.cache import cached_image

router = APIRouter(tags=["media"])


@router.get("/media/image/{hash}")
async def image(request: Request, hash: str) -> Response:
    """Serve bytes from the local cache, or a small transparent placeholder on cache failure."""
    if not request.app.state.config.image_cache_enabled:
        return _placeholder()
    found = await cached_image(request.app.state.engine, request.app.state.config.data_dir, hash)
    if found is None:
        return _placeholder()
    path, content_type = found
    return FileResponse(
        path, media_type=content_type, headers={"Cache-Control": "public, max-age=86400"}
    )


def _placeholder() -> Response:
    """Return a non-cacheable transparent GIF without disclosing remote image URLs."""
    return Response(
        content=_PLACEHOLDER, media_type="image/gif", headers={"Cache-Control": "no-store"}
    )


_PLACEHOLDER = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
