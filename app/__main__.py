"""Container entry point.

`python -m app` starts the server.
`python -m app backup` writes a consistent database copy to DATA_DIR/backups.
`python -m app restore FILE` replaces the database with a backup. Stop the server first.
`python -m app reset-admin` recovers a lost password.
"""

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from app.config import Settings
from app.context import build_context
from app.db import run_migrations
from app.security import reset_admin


def backup(settings: Settings) -> Path:
    """Online backup through SQLite's backup API. Safe while the application is running."""
    source_path = settings.data_dir / "monitor.db"
    target_dir = settings.data_dir / "backups"
    target_dir.mkdir(exist_ok=True)
    target_path = target_dir / f"monitor-{datetime.now(UTC):%Y%m%d-%H%M%S}.db"
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    target_path.chmod(0o600)
    return target_path


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


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "restore":
        settings = Settings()
        restore(settings, Path(sys.argv[2]))
        sys.stdout.write("Database restored. Start the application again.\n")
        return
    if sys.argv[1:] == ["backup"]:
        sys.stdout.write(f"Backup written to {backup(Settings())}\n")
        return
    if sys.argv[1:] == ["reset-admin"]:
        settings = Settings()
        run_migrations(settings.database_url)
        ctx = build_context(settings)
        reset_admin(ctx)
        ctx.close()
        sys.stdout.write("Administrator removed. Restart the container, then open /setup with the new token.\n")
        return
    # One worker only: the scheduler lives in this process (see docs/ARCHITECTURE.md).
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container-internal; the host binding is set in compose.yaml
        port=8080,
        workers=1,
        log_config=None,
        server_header=False,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
