"""Single-owner scheduler.

One background thread polls the database for due products. The schedule itself
(`products.next_check_at`) is persistent, so a restart resumes monitoring without
duplicate jobs. See docs/ARCHITECTURE.md for why this is safe with one worker.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import select

from app import budget, notify
from app.backup import run_scheduled
from app.checker import CheckResult, check_product
from app.context import AppContext
from app.models import Product

log = logging.getLogger(__name__)

STARTUP_SPREAD_SECONDS = 600.0
STALE_AFTER_TICKS = 10


def recover(ctx: AppContext) -> None:
    """Startup recovery: close interrupted requests and spread overdue checks to avoid a burst."""
    now = ctx.now()
    with ctx.sessions() as session:
        interrupted = budget.recover_interrupted(session)
        overdue = session.scalars(
            select(Product).where(
                Product.status == "active", (Product.next_check_at <= now) | Product.next_check_at.is_(None)
            )
        ).all()
        for product in overdue:
            product.next_check_at = now + timedelta(seconds=ctx.rng.uniform(5, STARTUP_SPREAD_SECONDS))
        session.commit()
    log.info("scheduler recovery complete", extra={"interrupted_requests": interrupted, "overdue": len(overdue)})


class Scheduler:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, ctx.settings.scheduler_batch_size), thread_name_prefix="check"
        )
        self.last_tick_at: datetime | None = None

    def start(self) -> None:
        recover(self.ctx)
        self.ctx.sleep = self._stop.wait  # retry waits end early on shutdown
        self.last_tick_at = self.ctx.now()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started", extra={"tick_seconds": self.ctx.settings.scheduler_tick_seconds})

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=25)
        self._pool.shutdown(wait=True, cancel_futures=True)
        log.info("scheduler stopped")

    @property
    def healthy(self) -> bool:
        if self._thread is None:
            return not self.ctx.settings.scheduler_enabled
        if not self._thread.is_alive():
            return False
        # A long provider call (Apify runs take minutes) may delay ticks; allow generous slack.
        slack = max(600.0, self.ctx.settings.scheduler_tick_seconds * STALE_AFTER_TICKS)
        return self.last_tick_at is not None and (self.ctx.now() - self.last_tick_at).total_seconds() < slack

    def due_product_ids(self) -> list[int]:
        with self.ctx.sessions() as session:
            query = (
                select(Product.id)
                .where(Product.status == "active", Product.next_check_at <= self.ctx.now())
                .order_by(Product.next_check_at)
                .limit(self.ctx.settings.scheduler_batch_size)
            )
            return list(session.scalars(query))

    def run_once(self) -> list[CheckResult]:
        """Check every due product (bounded batch) and wait for the batch to finish."""
        results = list(self._pool.map(lambda product_id: check_product(self.ctx, product_id), self.due_product_ids()))
        notify.deliver_pending(self.ctx)  # also retries deliveries that failed earlier
        try:
            run_scheduled(self.ctx.settings, self.ctx.now())
        except Exception:
            log.exception("scheduled backup failed")  # a failed backup must not stop price checks
        self.last_tick_at = self.ctx.now()
        return results

    def _loop(self) -> None:
        while not self._stop.wait(self.ctx.settings.scheduler_tick_seconds):
            try:
                self.run_once()
            except Exception:
                log.exception("scheduler tick failed")
