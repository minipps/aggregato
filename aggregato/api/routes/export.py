"""Stream an operator's private archive with configured credentials removed."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import Request

from aggregato.api.deps import require_operator
from aggregato.api.errors import ProblemError, error_type
from aggregato.export import build_archive, sqlite_database_path

router = APIRouter(tags=["operations"])


@router.get("/export", response_class=StreamingResponse, dependencies=[Depends(require_operator)])
async def export_archive(request: Request) -> StreamingResponse:
    if sqlite_database_path(request.app.state.config.database_url) is None:
        raise ProblemError(
            status=501,
            title="Portable backup is unavailable",
            detail=(
                "Portable backup requires an on-disk SQLite database. PostgreSQL and in-memory "
                "SQLite are unsupported."
            ),
            type=error_type("portable-backup-unavailable"),
        )
    path = await asyncio.to_thread(build_archive, request.app.state.config)
    return StreamingResponse(
        _file_chunks(path),
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="aggregato-export.zip"'},
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


def _file_chunks(path: Path) -> Iterator[bytes]:
    try:
        with path.open("rb") as archive:
            while chunk := archive.read(64 * 1024):
                yield chunk
    finally:
        path.unlink(missing_ok=True)
