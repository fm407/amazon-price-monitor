import random
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.context import AppContext, build_context
from app.db import run_migrations
from app.providers.mock import MockProvider

T0 = datetime(2026, 11, 18, 17, 30, tzinfo=UTC)
GPU = "B0DEMOGPU1"


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def refuse_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected outbound request to {request.url.host}")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, scheduler_enabled=False, log_level="WARNING")


def make_ctx(settings: Settings, **kwargs) -> AppContext:
    run_migrations(settings.database_url)
    kwargs.setdefault("http", httpx.Client(transport=httpx.MockTransport(refuse_network)))
    ctx = build_context(settings, **kwargs)
    ctx.now = Clock()
    ctx.sleep = lambda seconds: None
    ctx.rng = random.Random(7)
    return ctx


@pytest.fixture
def ctx(settings):
    context = make_ctx(settings)
    yield context
    context.close()


@pytest.fixture
def mock(ctx) -> MockProvider:
    return ctx.providers["mock"]
