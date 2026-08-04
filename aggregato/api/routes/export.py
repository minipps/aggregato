"""Stream a portable, secret-free archive download ."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from starlette.requests import Request

from aggregato.export import build_archive

router = APIRouter(tags=["operations"])


@router.get("/export", response_class=StreamingResponse)
async def export_archive(request: Request) -> StreamingResponse:
    path = await asyncio.to_thread(build_archive, request.app.state.config)
    return StreamingResponse(
        _file_chunks(path),
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="aggregato-export.zip"'},
    )


def _file_chunks(path: Path) -> Iterator[bytes]:
    try:
        with path.open("rb") as archive:
            while chunk := archive.read(64 * 1024):
                yield chunk
    finally:
        path.unlink(missing_ok=True)
