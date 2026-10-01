"""Portable archive creation and restoration.

Exports sanitize configured credentials and selected operational data.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.engine import make_url

from aggregato.config import Config, public_provider_settings

ARCHIVE_DATABASE = "aggregato.sqlite3"
ARCHIVE_CONFIG = "configuration.json"


def sqlite_database_path(database_url: str) -> Path | None:
    """Return the file path for a supported SQLite database, or ``None`` otherwise."""
    url = make_url(database_url)
    database = url.database
    mode = url.query.get("mode")
    memory_mode = mode == "memory" or (isinstance(mode, tuple) and "memory" in mode)
    if (
        url.get_backend_name() != "sqlite"
        or not database
        or database == ":memory:"
        or database.startswith("file:")
        or memory_mode
    ):
        return None
    return Path(database)


def build_archive(config: Config) -> Path:
    """Build a sanitized, transactionally consistent SQLite archive and public config."""
    source = sqlite_database_path(config.database_url)
    if source is None:
        raise ValueError("portable export currently requires an on-disk SQLite database")
    if not source.is_file():
        raise ValueError("database has not been created yet")

    descriptor, output_name = tempfile.mkstemp(prefix="aggregato-export-", suffix=".zip")
    os.close(descriptor)
    output = Path(output_name)
    try:
        with tempfile.TemporaryDirectory(prefix="aggregato-export-") as temp:
            snapshot = Path(temp) / ARCHIVE_DATABASE
            # SQLite's backup API includes committed WAL content, unlike a byte-for-byte file copy.
            with (
                closing(sqlite3.connect(source)) as src,
                closing(sqlite3.connect(snapshot)) as dest,
            ):
                src.backup(dest)
            _sanitize_snapshot(snapshot, config)
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, ARCHIVE_DATABASE)
                archive.writestr(
                    ARCHIVE_CONFIG, json.dumps(config.public_dict(), indent=2, sort_keys=True)
                )
                images = config.data_dir / "images"
                if images.is_dir():
                    for image in images.rglob("*"):
                        if image.is_file() and not image.is_symlink():
                            archive.write(image, image.relative_to(config.data_dir).as_posix())
        return output
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _sanitize_snapshot(snapshot: Path, config: Config) -> None:
    """Remove credentials and operational payloads while keeping browsable archive data."""
    with closing(sqlite3.connect(snapshot)) as database:
        database.execute("PRAGMA secure_delete = ON")
        for provider_id, raw_settings in database.execute("SELECT id, config FROM providers"):
            try:
                settings: object = json.loads(raw_settings or "{}")
            except (TypeError, json.JSONDecodeError):
                settings = {}
            safe = public_provider_settings(provider_id, settings, config.provider_dir)
            database.execute(
                "UPDATE providers SET config = ?, last_error = NULL, enabled = 0, "
                "status = 'disabled' "
                "WHERE id = ?",
                (json.dumps(safe), provider_id),
            )
        database.execute("DELETE FROM sessions")
        database.execute("DELETE FROM import_jobs")
        database.execute("DELETE FROM replay_jobs")
        database.execute("DELETE FROM ingest_failures")
        database.execute(
            "UPDATE sync_runs SET error_message = NULL, log_excerpt = NULL, log = NULL, "
            "raw_responses = NULL, cursor_before = NULL, cursor_after = NULL"
        )
        database.execute(
            "UPDATE provider_state SET cursor = NULL, kv = '{}', next_run_at = NULL, "
            "requested_mode = NULL, requested_lineage_id = NULL, now_playing_item = NULL, "
            "now_playing_changed_at = NULL, now_playing_checked_at = NULL, "
            "now_playing_next_poll_at = NULL, now_playing_failures = 0, "
            "now_playing_config_fingerprint = NULL"
        )
        database.commit()
        # Rebuild pages so removed secret strings are not left in SQLite's free-page area.
        database.execute("VACUUM")


def restore_archive(archive_path: Path, data_dir: Path) -> dict[str, Any]:
    """Validate and stage an archive before publishing it into an empty data directory."""
    if data_dir.exists() and (not data_dir.is_dir() or any(data_dir.iterdir())):
        raise ValueError("restore target must be an empty directory")
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{data_dir.name}-restore-", dir=data_dir.parent))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("archive contains duplicate members")
            if ARCHIVE_DATABASE not in names or ARCHIVE_CONFIG not in names:
                raise ValueError("not an Aggregato archive")

            config = json.loads(archive.read(ARCHIVE_CONFIG))
            if not isinstance(config, dict):
                raise ValueError("archive configuration must be a JSON object")

            root = stage.resolve()
            for info in infos:
                member = PurePosixPath(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise ValueError("archive contains an unsafe path")
                if info.filename not in {ARCHIVE_DATABASE, ARCHIVE_CONFIG} and not (
                    info.filename.startswith("images/")
                ):
                    raise ValueError("archive contains an unsupported member")
                target = stage.joinpath(*member.parts)
                if not target.resolve().is_relative_to(root):
                    raise ValueError("archive contains an unsafe path")

            database = stage / "aggregato.db"
            _extract_member(archive, archive.getinfo(ARCHIVE_DATABASE), database)
            for info in infos:
                if info.filename.startswith("images/"):
                    target = stage.joinpath(*PurePosixPath(info.filename).parts)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        _extract_member(archive, info, target)

        with closing(sqlite3.connect(database)) as restored:
            if restored.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("archive database is corrupt")

        if data_dir.exists():
            data_dir.rmdir()
        os.replace(stage, data_dir)
        return config
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _extract_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> None:
    if info.is_dir():
        raise ValueError(f"archive member {info.filename!r} must be a file")
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, target.open("xb") as destination:
        shutil.copyfileobj(source, destination)
