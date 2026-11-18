"""Demonstration data for mock mode. Every price here is fictional.

DEMO_MODE=true seeds three sample products by replaying scripted prices through
the real pipeline. DEMO_MODE=false removes them again at the next start.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select

from app import alerts, products
from app.checker import check_product
from app.context import AppContext
from app.models import AlertRule, Product
from app.money import from_minor
from app.providers.mock import MockProvider

log = logging.getLogger(__name__)

STEP = timedelta(hours=12)
# asin -> (rule kind, threshold, fictional price series, oldest first)
SCRIPTS: dict[str, tuple[str, str, list[str]]] = {
    "B0DEMOGPU1": (
        "pct_below_baseline", "25",
        ["800.00", "800.00", "789.00", "765.00", "779.00", "740.00", "699.00", "720.00", "655.00", "629.00", "599.00", "599.00"],
    ),
    "B0DEMOSSD1": (
        "pct_below_baseline", "15",
        ["260.00", "258.00", "262.00", "255.00", "249.99", "252.00", "244.00", "239.99", "246.00", "235.00", "228.00", "224.99"],
    ),
    "B0DEMORAM1": (
        "fixed_target", "110",
        ["140.00", "138.50", "139.99", "134.00", "129.99", "132.00", "124.00", "119.99", "121.50", "116.00", "112.00", "114.50"],
    ),
}  # fmt: skip


def _fixed(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def remove_demo_products(ctx: AppContext) -> int:
    with ctx.sessions() as session:
        rows = session.scalars(select(Product).where(Product.is_demo)).all()
        for product in rows:
            session.delete(product)
        session.commit()
        return len(rows)


def _restore_mock_prices(ctx: AppContext, mock: MockProvider) -> None:
    """The mock provider keeps prices in memory. After a restart, continue from the stored prices."""
    with ctx.sessions() as session:
        for product in session.scalars(select(Product).where(Product.latest_price_minor.is_not(None))):
            mock.set_price(product.asin, str(from_minor(product.latest_price_minor or 0, product.currency or "USD")))


def seed(ctx: AppContext) -> None:
    mock = ctx.providers.get("mock")
    if not isinstance(mock, MockProvider):
        return
    _restore_mock_prices(ctx, mock)
    with ctx.sessions() as session:
        if session.scalar(select(Product.id).where(Product.is_demo).limit(1)):
            return
    real_now, end = ctx.now, ctx.now()
    try:
        for asin, (kind, threshold, series) in SCRIPTS.items():
            start = end - STEP * (len(series) - 1)
            ctx.now = _fixed(start)
            mock.set_price(asin, series[0])
            with ctx.sessions() as session:
                product, _ = products.add_product(ctx, session, asin, is_demo=True)
                # Seeded alerts go to the mock channel only, so no real message is sent at startup.
                alerts.create_rule(session, product, kind, threshold, channels=["mock"], cooldown_minutes=0)
                alerts.create_rule(session, product, "new_low", None, channels=["mock"], cooldown_minutes=4320)
                session.commit()
                product_id = product.id
            for index, price in enumerate(series[1:], start=1):
                ctx.now = _fixed(start + STEP * index)
                mock.set_price(asin, price)
                check_product(ctx, product_id)
    finally:
        ctx.now = real_now
    with ctx.sessions() as session:
        for rule in session.scalars(select(AlertRule).join(Product).where(Product.is_demo)):
            rule.channels = []  # from now on, demo alerts use every enabled channel
            if rule.kind != "new_low":
                rule.cooldown_minutes = 60
        for product in session.scalars(select(Product).where(Product.is_demo)):
            product.next_check_at = end + timedelta(minutes=ctx.settings.default_check_interval_minutes)
        session.commit()
    log.info("demo data seeded", extra={"products": len(SCRIPTS)})


def sync(ctx: AppContext) -> None:
    """Called once at startup: seed in demo mode, otherwise remove leftover demo products."""
    if ctx.settings.demo_mode:
        seed(ctx)
        return
    removed = remove_demo_products(ctx)
    if removed:
        log.info("demo data removed", extra={"products": removed})
    mock = ctx.providers.get("mock")
    if isinstance(mock, MockProvider):
        _restore_mock_prices(ctx, mock)
