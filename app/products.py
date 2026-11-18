"""Product management: add, edit, delete, pause, resume, baseline, statistics, export."""

import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.amazon import parse_product_input
from app.checker import CheckResult, check_product, clear_pending, placeholder_title
from app.context import AppContext
from app.models import PriceObservation, Product
from app.money import from_minor

log = logging.getLogger(__name__)

ALLOWED_INTERVALS = (30, 60, 180, 360, 720, 1440)
RANGES = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}
FORMULA_PREFIXES = "=+-@\t\r"
EDITABLE = {"title", "provider", "check_interval_minutes", "seller_filter", "include_used"}


class DuplicateProduct(Exception):
    def __init__(self, product_id: int) -> None:
        super().__init__("This product is already tracked.")
        self.product_id = product_id


class ProductNotFound(Exception):
    pass


class InvalidProductUpdate(ValueError):
    pass


def validate_options(ctx: AppContext, values: dict[str, Any]) -> dict[str, Any]:
    """Validate the user-editable monitoring options. Returns only recognised keys."""
    clean = {key: value for key, value in values.items() if key in EDITABLE}
    interval = clean.get("check_interval_minutes")
    if interval is not None and not 5 <= int(interval) <= 10080:
        raise InvalidProductUpdate("The check interval must be between 5 minutes and 7 days.")
    provider = clean.get("provider")
    if provider and (provider not in ctx.providers or not ctx.providers[provider].configured):
        raise InvalidProductUpdate(f"Provider '{provider}' is not available or not configured in this deployment.")
    if "provider" in clean:
        clean["provider"] = provider or None
    if clean.get("seller_filter", "any") not in ("any", "amazon"):
        raise InvalidProductUpdate("seller_filter must be 'any' or 'amazon'.")
    if "title" in clean:
        title = " ".join(str(clean["title"] or "").split())  # single line: titles go into email subjects
        if not title or len(title) > 500:
            raise InvalidProductUpdate("The title must be between 1 and 500 characters.")
        clean["title"] = title
    return clean


def add_product(
    ctx: AppContext,
    session: Session,
    text: str,
    options: dict[str, Any] | None = None,
    *,
    is_demo: bool = False,
) -> tuple[Product, CheckResult]:
    """Create a product, then run its first check. A failed first check never sets a baseline."""
    identity = parse_product_input(text, client=ctx.http)
    clean = validate_options(ctx, options or {})
    clean.pop("title", None)
    existing = session.scalar(
        select(Product).where(Product.asin == identity.asin, Product.marketplace == identity.marketplace)
    )
    if existing:
        raise DuplicateProduct(existing.id)
    product = Product(
        asin=identity.asin,
        marketplace=identity.marketplace,
        title=placeholder_title(identity.asin),
        url=identity.url,
        is_demo=is_demo,
        created_at=ctx.now(),
        next_check_at=ctx.now(),
        **clean,
    )
    session.add(product)
    try:
        session.commit()
    except IntegrityError as exc:  # two concurrent adds of the same ASIN
        session.rollback()
        raise DuplicateProduct(0) from exc
    result = check_product(ctx, product.id)
    if result.category == "not_found":
        session.delete(product)
        session.commit()
        raise ProductNotFound("The provider could not find this product on Amazon.")
    session.refresh(product)
    log.info("product added", extra={"product_id": product.id, "asin": product.asin, "first_check": result.status})
    return product, result


def update_product(ctx: AppContext, product: Product, values: dict[str, Any]) -> None:
    for key, value in validate_options(ctx, values).items():
        setattr(product, key, value)


def set_status(ctx: AppContext, product: Product, status: str) -> None:
    product.status = status
    clear_pending(product)  # a held reading does not survive a pause or a resume
    if status == "active":
        product.consecutive_failures = 0
        product.next_check_at = ctx.now() + timedelta(seconds=ctx.rng.uniform(5, 60))


def set_baseline(product: Product, baseline_minor: int | None = None) -> None:
    """Set the baseline to an explicit value, or reset it to the latest valid price."""
    value = product.latest_price_minor if baseline_minor is None else baseline_minor
    if value is None or value <= 0:
        raise InvalidProductUpdate("There is no valid price to use as the baseline yet.")
    product.baseline_minor = value


def history(
    session: Session,
    product_id: int,
    now: datetime,
    range_key: str = "all",
    limit: int = 5000,
    offset: int = 0,
):
    query = select(PriceObservation).where(PriceObservation.product_id == product_id)
    if range_key in RANGES:
        query = query.where(PriceObservation.observed_at >= now - RANGES[range_key])
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(
        query.order_by(PriceObservation.observed_at, PriceObservation.id).limit(limit).offset(offset)
    )
    return list(rows), total


PRODUCT_EXPORT_FIELDS = (
    "id", "asin", "marketplace", "title", "url", "brand", "currency", "status", "provider", "baseline",
    "latest_price", "lowest", "highest", "availability", "created_at", "last_success_at", "is_demo",
)  # fmt: skip
HISTORY_EXPORT_FIELDS = (
    "observed_at", "price", "item_price", "shipping", "currency", "seller", "condition", "availability",
    "provider", "price_basis",
)  # fmt: skip


def _amount(minor: int | None, currency: str | None) -> str | None:
    return None if minor is None or not currency else str(from_minor(minor, currency))


def product_export_row(p: Product) -> dict[str, Any]:
    return {
        "id": p.id, "asin": p.asin, "marketplace": p.marketplace, "title": p.title, "url": p.url, "brand": p.brand,
        "currency": p.currency, "status": p.status, "provider": p.provider,
        "baseline": _amount(p.baseline_minor, p.currency), "latest_price": _amount(p.latest_price_minor, p.currency),
        "lowest": _amount(p.lowest_minor, p.currency), "highest": _amount(p.highest_minor, p.currency),
        "availability": p.availability, "created_at": p.created_at.isoformat(),
        "last_success_at": p.last_success_at.isoformat() if p.last_success_at else None, "is_demo": p.is_demo,
    }  # fmt: skip


def history_export_row(o: PriceObservation) -> dict[str, Any]:
    return {
        "observed_at": o.observed_at.isoformat(), "price": _amount(o.price_minor, o.currency),
        "item_price": _amount(o.item_price_minor, o.currency), "shipping": _amount(o.shipping_minor, o.currency),
        "currency": o.currency, "seller": o.seller, "condition": o.condition, "availability": o.availability,
        "provider": o.provider, "price_basis": o.price_basis,
    }  # fmt: skip


def to_csv(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        # Neutralise spreadsheet formula injection in user-influenced text such as titles.
        writer.writerow(
            {k: f"'{v}" if isinstance(v, str) and v and v[0] in FORMULA_PREFIXES else v for k, v in row.items()}
        )
    return buffer.getvalue()
