from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from app import models  # noqa: F401
from app.db import Base, make_engine, run_migrations


def test_migrations_build_schema_that_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    run_migrations(url)
    run_migrations(url)  # idempotent: upgrading an up-to-date database is a no-op
    engine = make_engine(url)
    with engine.connect() as conn:
        assert set(Base.metadata.tables) <= set(inspect(conn).get_table_names())
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
