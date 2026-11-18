"""Alembic environment. The URL comes from the Config (set by app.db) or DATABASE_URL."""

from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401 - registers tables on Base.metadata
from app.db import Base

target_metadata = Base.metadata


def _url() -> str:
    url = context.config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from app.config import Settings

    return Settings().database_url


def run_migrations() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        # render_as_batch lets SQLite apply ALTER TABLE changes in later migrations.
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
