"""Serve local cached images; no third-party redirect ever reaches the browser ."""

from __future__ import annotations

import re

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from starlette.requests import Request

from aggregato.db.retention import get_settings
from aggregato.images.cache import cached_image

router = APIRouter(tags=["media"])

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self'; object-src 'none'; frame-ancestors 'none'"
    ),
    "Content-Disposition": 'attachment; filename="image"',
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@router.get("/media/image/{hash}")
async def image(request: Request, hash: str) -> Response:
    """Serve bytes from the local cache, or a small transparent placeholder on cache failure."""
    # The URL is a content-addressed identifier, never an arbitrary filename or source URL. Keep
    # this check in the route as well as the cache helper so malformed public paths cannot cause a
    # database lookup or accidentally select a future route with a broader hash interpretation.
    if _DIGEST.fullmatch(hash) is None:
        return _placeholder()
    # The static config switch remains a hard opt-out for deployments that never want image
    # retrieval; the database setting is the operator-adjustable default.
    if not request.app.state.config.image_cache_enabled:
        return _placeholder()
    enabled = (await get_settings(request.app.state.engine))["image_cache_enabled"]
    found = await cached_image(
        request.app.state.engine, request.app.state.config.data_dir, hash, enabled=enabled
    )
    if found is None:
        return _placeholder()
    path, content_type = found
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            **_SECURITY_HEADERS,
            "Cache-Control": "public, max-age=86400",
        },
    )


def _placeholder() -> Response:
    """Return a non-cacheable transparent GIF without disclosing remote image URLs."""
    return Response(
        content=_PLACEHOLDER,
        media_type="image/gif",
        headers={**_SECURITY_HEADERS, "Cache-Control": "no-store"},
    )


_PLACEHOLDER = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
