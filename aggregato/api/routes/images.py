"""Serve local cached images; no third-party redirect ever reaches the browser ."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from starlette.requests import Request

from aggregato.api.clock import now as request_now
from aggregato.db.engine import transaction
from aggregato.db.retention import get_settings
from aggregato.db.schema import providers
from aggregato.images.cache import UnsafeImageURL, cached_image, image_origin

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
    allowed_origins = await _configured_image_origins(request)
    found = await cached_image(
        request.app.state.engine,
        request.app.state.config.data_dir,
        hash,
        enabled=enabled,
        now=request_now(request),
        allowed_origins=allowed_origins,
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


async def _configured_image_origins(request: Request) -> frozenset[str]:
    """Return origins explicitly configured as provider endpoints.

    A provider can be a self-hosted service on the deployment's private network, as Koito is in
    the Docker stack. The image cache still rejects private destinations by default; this narrow
    exception is based only on an operator-owned ``base_url`` and matches scheme, host, and port.
    A provider payload cannot add an origin to this set, and redirects must pass the same check.
    Database settings take precedence because they are the mutable provider configuration layer.
    """
    config = request.app.state.config
    fallbacks: dict[str, Mapping[str, Any]] = {
        provider_id: provider.settings for provider_id, provider in config.providers.items()
    }
    async with transaction(request.app.state.engine) as conn:
        rows = list(await conn.execute(select(providers.c.id, providers.c.config)))

    origins: set[str] = set()
    seen: set[str] = set()
    for row in rows:
        seen.add(str(row.id))
        settings: object = (
            row.config
            if isinstance(row.config, Mapping) and row.config
            else fallbacks.get(str(row.id))
        )
        _add_base_url_origin(settings, origins)
    for provider_id, settings in fallbacks.items():
        if provider_id not in seen:
            _add_base_url_origin(settings, origins)
    return frozenset(origins)


def _add_base_url_origin(settings: object, origins: set[str]) -> None:
    if not isinstance(settings, Mapping):
        return
    base_url = settings.get("base_url")
    if not isinstance(base_url, str):
        return
    try:
        origins.add(image_origin(base_url))
    except UnsafeImageURL:
        # A malformed provider block is already isolated elsewhere; it must not make the image
        # route fail, nor should it create a trust exception for a value the cache cannot parse.
        return


_PLACEHOLDER = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
