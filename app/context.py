"""Application context: the explicit dependency container shared by web, API and scheduler."""

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import make_engine, utcnow
from app.notify import Notifier, build_notifiers
from app.providers import PriceProvider, build_providers


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    http: httpx.Client
    providers: dict[str, PriceProvider]
    notifiers: dict[str, Notifier] = field(default_factory=dict)
    now: Callable[[], datetime] = utcnow
    sleep: Callable[[float], Any] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    # Single-process guards. They are valid because exactly one worker process runs.
    budget_lock: threading.Lock = field(default_factory=threading.Lock)
    delivery_lock: threading.Lock = field(default_factory=threading.Lock)
    blocked_until: dict[str, datetime] = field(default_factory=dict)
    _inflight: set[int] = field(default_factory=set)
    _inflight_lock: threading.Lock = field(default_factory=threading.Lock)
    _slots: dict[str, threading.BoundedSemaphore] = field(default_factory=dict)

    def try_begin_check(self, product_id: int) -> bool:
        with self._inflight_lock:
            if product_id in self._inflight:
                return False
            self._inflight.add(product_id)
            return True

    def end_check(self, product_id: int) -> None:
        with self._inflight_lock:
            self._inflight.discard(product_id)

    def provider_slot(self, name: str) -> threading.BoundedSemaphore:
        with self._inflight_lock:
            if name not in self._slots:
                self._slots[name] = threading.BoundedSemaphore(max(1, self.providers[name].max_concurrency))
            return self._slots[name]

    def close(self) -> None:
        self.http.close()
        self.engine.dispose()


def build_context(
    settings: Settings,
    *,
    http: httpx.Client | None = None,
    providers: dict[str, PriceProvider] | None = None,
    notifiers: dict[str, Notifier] | None = None,
) -> AppContext:
    engine = make_engine(settings.database_url)
    http = http or httpx.Client(
        timeout=settings.http_timeout_seconds,
        follow_redirects=False,
        headers={"User-Agent": "amazon-price-monitor"},
    )
    return AppContext(
        settings=settings,
        engine=engine,
        sessions=sessionmaker(engine, expire_on_commit=False),
        http=http,
        providers=providers if providers is not None else build_providers(settings, http),
        notifiers=notifiers if notifiers is not None else build_notifiers(settings, http),
    )
