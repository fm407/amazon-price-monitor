"""Relational model.

Money is stored as integer minor currency units (cents for USD). API cost is
stored as integer micro-USD. No binary floating point is used for money.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow

# Alert rule kinds
PCT_BELOW_BASELINE = "pct_below_baseline"
FIXED_TARGET = "fixed_target"
PCT_BELOW_AVERAGE = "pct_below_average"
NEW_LOW = "new_low"
PCT_DROP_SINCE_LAST = "pct_drop_since_last"
BACK_IN_STOCK = "back_in_stock"
RULE_KINDS = (PCT_BELOW_BASELINE, FIXED_TARGET, PCT_BELOW_AVERAGE, NEW_LOW, PCT_DROP_SINCE_LAST, BACK_IN_STOCK)
NO_THRESHOLD_KINDS = (NEW_LOW, BACK_IN_STOCK)
PERCENT_KINDS = (PCT_BELOW_BASELINE, PCT_BELOW_AVERAGE, PCT_DROP_SINCE_LAST)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("asin", "marketplace"),
        Index("ix_products_status_next_check_at", "status", "next_check_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asin: Mapped[str] = mapped_column(String(10))
    marketplace: Mapped[str] = mapped_column(String(4), default="US")
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(200))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    brand: Mapped[str | None] = mapped_column(String(200))
    currency: Mapped[str | None] = mapped_column(String(3))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)

    # Monitoring configuration
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | paused
    provider: Mapped[str | None] = mapped_column(String(20))  # None = application default
    check_interval_minutes: Mapped[int | None] = mapped_column(Integer)  # None = application default
    seller_filter: Mapped[str] = mapped_column(String(10), default="any")  # any | amazon
    include_used: Mapped[bool] = mapped_column(Boolean, default=False)

    # Price state. Only valid observations update these fields.
    baseline_minor: Mapped[int | None] = mapped_column(Integer)
    initial_price_minor: Mapped[int | None] = mapped_column(Integer)
    latest_price_minor: Mapped[int | None] = mapped_column(Integer)
    previous_price_minor: Mapped[int | None] = mapped_column(Integer)
    lowest_minor: Mapped[int | None] = mapped_column(Integer)
    highest_minor: Mapped[int | None] = mapped_column(Integer)
    latest_seller: Mapped[str | None] = mapped_column(String(200))
    latest_condition: Mapped[str | None] = mapped_column(String(10))
    availability: Mapped[str] = mapped_column(String(15), default="unknown")

    # A sudden large drop that waits for a second reading before it is stored (see CONFIRM_DROP_PERCENT).
    pending_price_minor: Mapped[int | None] = mapped_column(Integer)
    pending_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    pending_provider: Mapped[str | None] = mapped_column(String(20))

    # Check state
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_price_change_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    rules: Mapped[list["AlertRule"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", passive_deletes=True, order_by="AlertRule.id"
    )


class PriceObservation(Base):
    __tablename__ = "price_observations"
    __table_args__ = (Index("ix_price_observations_product_id_observed_at", "product_id", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    price_minor: Mapped[int] = mapped_column(Integer)  # the monitored price (see price_basis)
    item_price_minor: Mapped[int] = mapped_column(Integer)
    shipping_minor: Mapped[int | None] = mapped_column(Integer)  # None = unknown, never assumed free
    effective_minor: Mapped[int | None] = mapped_column(Integer)  # item + shipping when known
    list_price_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    seller: Mapped[str | None] = mapped_column(String(200))
    condition: Mapped[str] = mapped_column(String(10), default="unknown")
    availability: Mapped[str] = mapped_column(String(15), default="unknown")
    provider: Mapped[str] = mapped_column(String(20))
    price_basis: Mapped[str] = mapped_column(String(40))
    coupon_text: Mapped[str | None] = mapped_column(String(200))  # informational, never subtracted
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(25))
    # Percent kinds: basis points (25% = 2500). fixed_target: minor units. new_low: unused.
    threshold: Mapped[int] = mapped_column(Integer, default=0)
    lookback_days: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    channels: Mapped[list[str]] = mapped_column(JSON, default=list)  # empty = every enabled channel
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    reminder_minutes: Mapped[int | None] = mapped_column(Integer)  # None = no reminders
    further_drop_bp: Mapped[int | None] = mapped_column(Integer)  # None = no further-drop alerts

    # State
    is_below: Mapped[bool] = mapped_column(Boolean, default=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_trigger_price_minor: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    product: Mapped[Product] = relationship(back_populates="rules")


class AlertEvent(Base):
    __tablename__ = "alert_events"
    # One event per rule and observation: the idempotency guard against duplicate alerts.
    __table_args__ = (UniqueConstraint("rule_id", "observation_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("alert_rules.id", ondelete="SET NULL"))
    observation_id: Mapped[int | None] = mapped_column(ForeignKey("price_observations.id", ondelete="SET NULL"))
    rule_kind: Mapped[str] = mapped_column(String(25))
    event_type: Mapped[str] = mapped_column(String(15))  # trigger | further_drop | reminder
    price_minor: Mapped[int] = mapped_column(Integer)
    reference_minor: Mapped[int | None] = mapped_column(Integer)
    target_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    product: Mapped[Product] = relationship()


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "channel"),
        Index("ix_notification_deliveries_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("alert_events.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | retrying | sent | failed
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)  # plain text; channels escape it themselves
    image_url: Mapped[str | None] = mapped_column(String(1000))
    link_url: Mapped[str | None] = mapped_column(String(200))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ProviderRequest(Base):
    __tablename__ = "provider_requests"
    __table_args__ = (Index("ix_provider_requests_provider_started_at", "provider", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    asin: Mapped[str] = mapped_column(String(10))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    success: Mapped[bool | None] = mapped_column(Boolean)  # None = in flight
    http_status: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(30), default="pending")
    detail: Mapped[str | None] = mapped_column(String(300))
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    estimated_cost_micro: Mapped[int] = mapped_column(Integer, default=0)
    actual_cost_micro: Mapped[int | None] = mapped_column(Integer)  # only when the provider reports it
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class ApplicationSetting(Base):
    __tablename__ = "application_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
