"""Portable, secret-free archive creation and restoration (T130)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

from sqlalchemy.engine import make_url

from aggregato.config import Config

ARCHIVE_DATABASE = "aggregato.sqlite3"
ARCHIVE_CONFIG = "configuration.json"


def build_archive(config: Config) -> Path:
    """Build a ZIP archive containing a transactionally consistent SQLite copy and public config."""
    url = make_url(config.database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("portable export currently requires an on-disk SQLite database")
    source = Path(url.database)
    if not source.is_file():
        raise ValueError("database has not been created yet")
    output = Path(tempfile.mkstemp(prefix="aggregato-export-", suffix=".zip")[1])
    with tempfile.TemporaryDirectory(prefix="aggregato-export-") as temp:
        snapshot = Path(temp) / ARCHIVE_DATABASE
        # SQLite's backup API includes committed WAL content, unlike a byte-for-byte file copy.
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(snapshot)) as dest:
            src.backup(dest)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, ARCHIVE_DATABASE)
            archive.writestr(
                ARCHIVE_CONFIG, json.dumps(config.public_dict(), indent=2, sort_keys=True)
            )
            images = config.data_dir / "images"
            if images.is_dir():
                for image in images.rglob("*"):
                    if image.is_file():
                        archive.write(image, image.relative_to(config.data_dir).as_posix())
    return output


def restore_archive(archive_path: Path, data_dir: Path) -> dict[str, object]:
    """Restore an export into an empty data directory without ever contacting a provider."""
    data_dir.mkdir(parents=True, exist_ok=True)
    database = data_dir / "aggregato.db"
    if database.exists():
        raise ValueError("restore target already contains an archive")
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        if ARCHIVE_DATABASE not in names or ARCHIVE_CONFIG not in names:
            raise ValueError("not an Aggregato archive")
        for info in archive.infolist():
            target = data_dir / info.filename
            if not target.resolve().is_relative_to(data_dir.resolve()):
                raise ValueError("archive contains an unsafe path")
        with archive.open(ARCHIVE_DATABASE) as source, database.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        for info in archive.infolist():
            if info.filename.startswith("images/") and not info.is_dir():
                target = data_dir / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
        return json.loads(archive.read(ARCHIVE_CONFIG))  # type: ignore[no-any-return]
