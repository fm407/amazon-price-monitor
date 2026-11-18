"""Application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app import __version__
from app.config import Settings
from app.db import make_engine
from app.logging_config import configure_logging

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.secret_values())
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info("application startup", extra={"version": __version__, "provider": settings.provider})
        yield
        engine.dispose()
        log.info("application shutdown")

    app = FastAPI(title="Amazon Price Monitor", version=__version__, lifespan=lifespan)

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"success": True, "data": {"status": "ok", "version": __version__}, "error": None}

    return app
