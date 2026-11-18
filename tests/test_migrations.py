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


def test_upgrade_from_the_first_schema_keeps_existing_data(tmp_path):
    from alembic import command

    from app.db import alembic_config

    url = f"sqlite:///{tmp_path / 'old.db'}"
    command.upgrade(alembic_config(url), "0001")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO products (asin, marketplace, title, url, is_demo, status, seller_filter, include_used,"
                " availability, created_at, consecutive_failures, latest_price_minor)"
                " VALUES ('B0OLDDATA1', 'US', 'Kept', 'https://www.amazon.com/dp/B0OLDDATA1', 0, 'active', 'any', 0,"
                " 'in_stock', '2026-11-01 00:00:00', 0, 12345)"
            )
        )
    engine.dispose()
    run_migrations(url)
    engine = make_engine(url)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT title, latest_price_minor, pending_price_minor FROM products")).one()
        assert tuple(row) == ("Kept", 12345, None)
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    engine.dispose()
