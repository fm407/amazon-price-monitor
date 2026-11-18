import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, scheduler_enabled=False, log_level="WARNING")
