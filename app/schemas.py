"""REST request and response models. Money is exchanged as decimal strings plus a currency code."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app import alerts
from app.models import AlertEvent, AlertRule, NotificationDelivery, PriceObservation, Product, ProviderRequest
from app.money import from_minor, micro_to_usd, percent_of

RuleKind = Literal[
    "pct_below_baseline", "fixed_target", "pct_below_average", "new_low", "pct_drop_since_last", "back_in_stock"
]
Channel = Literal["discord", "email", "mock"]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class Meta(BaseModel):
    total: int
    page: int
    limit: int


class Envelope[T](BaseModel):
    success: bool = True
    data: T | None = None
    error: ErrorBody | None = None
    meta: Meta | None = None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductCreate(Strict):
    input: str = Field(min_length=10, max_length=2000, description="Amazon product URL, short link or ASIN")
    discount_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    target_price: Decimal | None = Field(default=None, gt=0)
    check_interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    provider: str | None = Field(default=None, max_length=20)
    seller_filter: Literal["any", "amazon"] = "any"
    include_used: bool = False


class ProductUpdate(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    check_interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    provider: str | None = Field(default=None, max_length=20)
    seller_filter: Literal["any", "amazon"] | None = None
    include_used: bool | None = None


class BaselineUpdate(Strict):
    price: Decimal | None = Field(default=None, gt=0, description="Omit to reset to the latest valid price")


class AlertCreate(Strict):
    product_id: int
    kind: RuleKind
    threshold: Decimal | None = Field(default=None, description="Percent, or a price for fixed_target")
    enabled: bool = True
    channels: list[Channel] = []
    cooldown_minutes: int = Field(default=60, ge=0, le=43200)
    lookback_days: int = Field(default=30, ge=1, le=3650)
    reminder_minutes: int | None = Field(default=None, ge=10, le=43200)
    further_drop_percent: Decimal | None = Field(default=None, gt=0, lt=100)


class AlertUpdate(Strict):
    threshold: Decimal | None = None
    enabled: bool | None = None
    channels: list[Channel] | None = None
    cooldown_minutes: int | None = Field(default=None, ge=0, le=43200)
    lookback_days: int | None = Field(default=None, ge=1, le=3650)
    reminder_minutes: int | None = Field(default=None, ge=0, le=43200)
    further_drop_percent: Decimal | None = Field(default=None, ge=0, lt=100)


class ProviderTest(Strict):
    input: str = Field(min_length=10, max_length=2000, description="Amazon product URL, short link or ASIN")


class MockControl(Strict):
    price: Decimal | None = None
    mode: Literal["ok", "outage", "rate_limit", "invalid", "no_price", "out_of_stock", "not_found", "auth"] | None = (
        None
    )
    shipping: Decimal | None = None
    shipping_unknown: bool = False
    seller: str | None = Field(default=None, max_length=200)
    condition: Literal["new", "used", "unknown"] | None = None


class AlertOut(BaseModel):
    id: int
    product_id: int
    kind: str
    description: str
    threshold: Decimal
    enabled: bool
    channels: list[str]
    cooldown_minutes: int
    lookback_days: int
    reminder_minutes: int | None
    further_drop_percent: Decimal | None
    is_below: bool
    last_triggered_at: datetime | None
    last_trigger_price: Decimal | None


class ProductOut(BaseModel):
    id: int
    asin: str
    marketplace: str
    title: str
    url: str
    image_url: str | None
    brand: str | None
    currency: str | None
    is_demo: bool
    status: str
    provider: str | None
    check_interval_minutes: int | None
    seller_filter: str
    include_used: bool
    initial_price: Decimal | None
    baseline_price: Decimal | None
    latest_price: Decimal | None
    previous_price: Decimal | None
    lowest_price: Decimal | None
    highest_price: Decimal | None
    target_price: Decimal | None
    pending_price: Decimal | None
    change_from_baseline_percent: Decimal | None
    latest_seller: str | None
    latest_condition: str | None
    availability: str
    created_at: datetime
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_price_change_at: datetime | None
    next_check_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    alerts: list[AlertOut]


class ObservationOut(BaseModel):
    id: int
    observed_at: datetime
    price: Decimal
    item_price: Decimal
    shipping: Decimal | None
    effective_price: Decimal | None
    list_price: Decimal | None
    currency: str
    seller: str | None
    condition: str
    availability: str
    provider: str
    price_basis: str
    coupon_text: str | None


class HistoryOut(BaseModel):
    currency: str | None
    baseline_price: Decimal | None
    target_price: Decimal | None
    observations: list[ObservationOut]


class EventOut(BaseModel):
    id: int
    product_id: int
    rule_id: int | None
    rule_kind: str
    event_type: str
    price: Decimal
    reference_price: Decimal | None
    target_price: Decimal | None
    currency: str
    created_at: datetime


class DeliveryOut(BaseModel):
    id: int
    event_id: int | None
    channel: str
    status: str
    subject: str
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None
    next_attempt_at: datetime | None


class ProviderRequestOut(BaseModel):
    id: int
    provider: str
    product_id: int | None
    asin: str
    started_at: datetime
    success: bool | None
    http_status: int | None
    category: str
    detail: str | None
    estimated_cost_usd: Decimal
    actual_cost_usd: Decimal | None
    retry_count: int
    duration_ms: int | None


def _money(minor: int | None, currency: str | None) -> Decimal | None:
    return None if minor is None or not currency else from_minor(minor, currency)


def alert_out(rule: AlertRule, currency: str) -> AlertOut:
    percent_kind = rule.kind in alerts.PERCENT_KINDS
    return AlertOut(
        id=rule.id,
        product_id=rule.product_id,
        kind=rule.kind,
        description=alerts.describe(rule, currency),
        threshold=Decimal(rule.threshold) / 100 if percent_kind else from_minor(rule.threshold, currency),
        enabled=rule.enabled,
        channels=list(rule.channels or []),
        cooldown_minutes=rule.cooldown_minutes,
        lookback_days=rule.lookback_days,
        reminder_minutes=rule.reminder_minutes,
        further_drop_percent=None if rule.further_drop_bp is None else Decimal(rule.further_drop_bp) / 100,
        is_below=rule.is_below,
        last_triggered_at=rule.last_triggered_at,
        last_trigger_price=_money(rule.last_trigger_price_minor, currency),
    )


def change_percent(product: Product) -> Decimal | None:
    """Signed change of the latest price against the baseline. Negative means cheaper."""
    if product.latest_price_minor is None or not product.baseline_minor:
        return None
    return percent_of(product.latest_price_minor - product.baseline_minor, product.baseline_minor)


def product_out(product: Product) -> ProductOut:
    currency = alerts.rule_currency(product)
    fields = {
        name: getattr(product, name)
        for name in (
            "id", "asin", "marketplace", "title", "url", "image_url", "brand", "currency", "is_demo", "status",
            "provider", "check_interval_minutes", "seller_filter", "include_used", "latest_seller",
            "latest_condition", "availability", "created_at", "last_attempt_at", "last_success_at",
            "last_price_change_at", "next_check_at", "consecutive_failures", "last_error",
        )
    }  # fmt: skip
    return ProductOut(
        **fields,
        initial_price=_money(product.initial_price_minor, currency),
        baseline_price=_money(product.baseline_minor, currency),
        latest_price=_money(product.latest_price_minor, currency),
        previous_price=_money(product.previous_price_minor, currency),
        lowest_price=_money(product.lowest_minor, currency),
        highest_price=_money(product.highest_minor, currency),
        target_price=_money(alerts.primary_target(product), currency),
        pending_price=_money(product.pending_price_minor, currency),
        change_from_baseline_percent=change_percent(product),
        alerts=[alert_out(rule, currency) for rule in product.rules],
    )


def observation_out(o: PriceObservation) -> ObservationOut:
    return ObservationOut(
        id=o.id,
        observed_at=o.observed_at,
        price=from_minor(o.price_minor, o.currency),
        item_price=from_minor(o.item_price_minor, o.currency),
        shipping=_money(o.shipping_minor, o.currency),
        effective_price=_money(o.effective_minor, o.currency),
        list_price=_money(o.list_price_minor, o.currency),
        currency=o.currency,
        seller=o.seller,
        condition=o.condition,
        availability=o.availability,
        provider=o.provider,
        price_basis=o.price_basis,
        coupon_text=o.coupon_text,
    )


def event_out(e: AlertEvent) -> EventOut:
    return EventOut(
        id=e.id,
        product_id=e.product_id,
        rule_id=e.rule_id,
        rule_kind=e.rule_kind,
        event_type=e.event_type,
        price=from_minor(e.price_minor, e.currency),
        reference_price=_money(e.reference_minor, e.currency),
        target_price=_money(e.target_minor, e.currency),
        currency=e.currency,
        created_at=e.created_at,
    )


def delivery_out(d: NotificationDelivery) -> DeliveryOut:
    return DeliveryOut.model_validate(d, from_attributes=True)


def request_out(r: ProviderRequest) -> ProviderRequestOut:
    return ProviderRequestOut(
        id=r.id,
        provider=r.provider,
        product_id=r.product_id,
        asin=r.asin,
        started_at=r.started_at,
        success=r.success,
        http_status=r.http_status,
        category=r.category,
        detail=r.detail,
        estimated_cost_usd=micro_to_usd(r.estimated_cost_micro),
        actual_cost_usd=None if r.actual_cost_micro is None else micro_to_usd(r.actual_cost_micro),
        retry_count=r.retry_count,
        duration_ms=r.duration_ms,
    )
