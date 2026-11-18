"""Container entry point: `python -m app`. `python -m app reset-admin` recovers a lost password."""

import sys

import uvicorn

from app.config import Settings
from app.context import build_context
from app.db import run_migrations
from app.security import reset_admin


def main() -> None:
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
