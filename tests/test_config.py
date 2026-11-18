import re
from pathlib import Path

from app.config import Settings

EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"
COMPOSE_ONLY = {"BIND_ADDRESS", "HOST_PORT"}


def test_env_example_documents_every_setting_and_parses():
    text = EXAMPLE.read_text()
    documented = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", text, re.M))
    fields = {name.upper() for name in Settings.model_fields}
    assert fields - documented == set()
    assert documented - fields - COMPOSE_ONLY == set()
    settings = Settings(_env_file=EXAMPLE)
    assert (settings.provider, settings.demo_mode, settings.allow_paid_api_requests) == ("mock", True, False)
    assert settings.apify_max_charge_per_run_usd is None and settings.notify_mock_enabled is None
    assert settings.mock_notifier_enabled is True


def test_example_file_contains_no_real_secrets():
    for line in EXAMPLE.read_text().splitlines():
        key, _, value = line.partition("=")
        if any(word in key for word in ("KEY", "TOKEN", "PASSWORD", "WEBHOOK")) and not key.startswith("#"):
            assert value == "", key


def test_safe_defaults():
    settings = Settings(_env_file=None)
    assert settings.provider == "mock" and settings.allow_paid_api_requests is False
    assert settings.default_check_interval_minutes == 360 and settings.timezone == "America/New_York"
    assert settings.secret_values() == []


def test_backup_command_writes_a_consistent_copy(ctx):
    import sqlite3

    from app.backup import backup
    from app.models import Product

    with ctx.sessions() as session:
        session.add(Product(asin="B0BACKUP01", title="Backup me", url="https://www.amazon.com/dp/B0BACKUP01"))
        session.commit()
    path = backup(ctx.settings, ctx.now())
    assert path.parent.name == "backups" and path.stat().st_mode & 0o777 == 0o600
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT title FROM products").fetchall() == [("Backup me",)]
    assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    copy.close()


def test_restore_command_replaces_the_database_and_rejects_bad_files(ctx, tmp_path):
    import pytest
    from sqlalchemy import func, select

    from app.backup import backup, restore
    from app.models import Product

    with ctx.sessions() as session:
        session.add(Product(asin="B0BACKUP01", title="Keep me", url="https://www.amazon.com/dp/B0BACKUP01"))
        session.commit()
    saved = backup(ctx.settings, ctx.now())
    with ctx.sessions() as session:
        session.delete(session.scalars(select(Product)).one())
        session.add(Product(asin="B0LATER001", title="Added later", url="https://www.amazon.com/dp/B0LATER001"))
        session.commit()
    ctx.engine.dispose()
    restore(ctx.settings, saved)
    with ctx.sessions() as session:
        assert [p.title for p in session.scalars(select(Product))] == ["Keep me"]
        assert session.scalar(select(func.count()).select_from(Product)) == 1
    junk = tmp_path / "junk.db"
    junk.write_text("this is not a database")
    with pytest.raises((SystemExit, Exception)):
        restore(ctx.settings, junk)
    with ctx.sessions() as session:
        assert [p.title for p in session.scalars(select(Product))] == ["Keep me"]  # untouched by the failed restore
