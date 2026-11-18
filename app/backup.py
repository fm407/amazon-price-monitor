"""Database backup and restore through SQLite's online backup API."""

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.db import run_migrations

log = logging.getLogger(__name__)

PREFIX, MANUAL_PREFIX, SUFFIX, STAMP = "monitor-", "manual-", ".db", "%Y%m%d-%H%M%S"


def backup_dir(settings: Settings) -> Path:
    return settings.data_dir / "backups"


def list_backups(settings: Settings) -> list[tuple[datetime, Path]]:
    """Existing backups, oldest first, with the time encoded in each file name."""
    found = []
    for path in backup_dir(settings).glob(f"{PREFIX}*{SUFFIX}"):
        try:
            found.append((datetime.strptime(path.name[len(PREFIX) : -len(SUFFIX)], STAMP), path))
        except ValueError:
            continue  # not one of ours: never touched
    return sorted(found)


def backup(settings: Settings, now: datetime, *, manual: bool = False) -> Path:
    """Write a consistent copy of the database. Safe while the application is running.

    The copy is written under a temporary name and renamed when complete, so a failed backup
    never leaves a file that looks like a backup. Manual backups get their own name and are
    not part of the automatic retention.
    """
    target_dir = backup_dir(settings)
    target_dir.mkdir(mode=0o700, exist_ok=True)
    name = f"{MANUAL_PREFIX if manual else PREFIX}{now:{STAMP}}{SUFFIX}"
    target_path, temporary = target_dir / name, target_dir / (name + ".tmp")
    os.close(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))  # private from the first byte
    source = sqlite3.connect(settings.data_dir / "monitor.db")
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
    except BaseException:
        target.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        target.close()
        source.close()
    os.replace(temporary, target_path)
    return target_path


def prune(settings: Settings, keep: int) -> int:
    """Delete the oldest scheduled backups beyond `keep`. Manual backups and other files are never touched."""
    existing = list_backups(settings)
    surplus = existing[: max(0, len(existing) - max(1, keep))]
    for _stamp, path in surplus:
        path.unlink(missing_ok=True)
    return len(surplus)


def run_scheduled(settings: Settings, now: datetime) -> Path | None:
    """Create a backup when the newest one is older than the configured interval."""
    if settings.backup_interval_hours <= 0:
        return None
    existing = list_backups(settings)
    naive_now = now.replace(tzinfo=None)
    if existing and naive_now - existing[-1][0] < timedelta(hours=settings.backup_interval_hours):
        return None
    path = backup(settings, now)
    removed = prune(settings, settings.backup_keep)
    log.info("scheduled backup written", extra={"file": path.name, "old_backups_removed": removed})
    return path


def restore(settings: Settings, backup_path: Path) -> None:
    """Replace the live database with a backup file. Run this only while the server is stopped."""
    # immutable=1: read the file as-is, without creating WAL side files next to it (it may be mounted read-only)
    source = sqlite3.connect(f"file:{backup_path}?mode=ro&immutable=1", uri=True)
    try:
        if source.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise SystemExit("The backup file failed the SQLite integrity check. Nothing was changed.")
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"products", "alembic_version"} <= tables:
            raise SystemExit("This file is not an Amazon Price Monitor backup. Nothing was changed.")
        target = sqlite3.connect(settings.data_dir / "monitor.db")
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    run_migrations(settings.database_url)  # an older backup is upgraded to the current schema
