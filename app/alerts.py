"""Alert rules: threshold maths, state transitions and event creation.

Threshold rules (percent below baseline, fixed target, percent below average)
alert when the price crosses from above to below the target. They stay quiet
while the price remains below, apart from optional reminders and further-drop
alerts, and become eligible again once the price rises above the target.
Event rules (new low, percent drop since last check) alert on each occurrence.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.amazon import MARKETPLACES
from app.models import (
    BACK_IN_STOCK,
    FIXED_TARGET,
    NEW_LOW,
    NO_THRESHOLD_KINDS,
    PCT_BELOW_AVERAGE,
    PCT_BELOW_BASELINE,
    PCT_DROP_SINCE_LAST,
    PERCENT_KINDS,
    RULE_KINDS,
    AlertEvent,
    AlertRule,
    PriceObservation,
    Product,
)
from app.money import apply_discount, format_money, parse_amount, to_minor

log = logging.getLogger(__name__)

MIN_AVERAGE_OBSERVATIONS = 5
STATE_KINDS = (PCT_BELOW_BASELINE, FIXED_TARGET, PCT_BELOW_AVERAGE)
KIND_LABELS = {
    PCT_BELOW_BASELINE: "Percent below baseline",
    FIXED_TARGET: "Fixed target price",
    PCT_BELOW_AVERAGE: "Percent below historical average",
    NEW_LOW: "New historical low",
    PCT_DROP_SINCE_LAST: "Percent drop since last check",
    BACK_IN_STOCK: "Back in stock",
}


def average_minor(
    session: Session, product_id: int, since: datetime | None = None, before_id: int | None = None
) -> tuple[int | None, int]:
    """(average, count) of stored observations, optionally limited to a lookback window."""
    query = select(func.avg(PriceObservation.price_minor), func.count()).where(
        PriceObservation.product_id == product_id
    )
    if since is not None:
        query = query.where(PriceObservation.observed_at >= since)
    if before_id is not None:
        query = query.where(PriceObservation.id < before_id)
    average, count = session.execute(query).one()
    return (None if average is None else int(round(average))), count


class InvalidRule(ValueError):
    pass


def rule_currency(product: Product) -> str:
    return product.currency or MARKETPLACES[product.marketplace].currency


def encode_threshold(kind: str, value: object, currency: str) -> int:
    """Percent kinds -> basis points; fixed target -> minor units; kinds without a threshold -> 0."""
    if kind not in RULE_KINDS:
        raise InvalidRule(f"Unknown alert type: {kind}")
    if kind in NO_THRESHOLD_KINDS:
        return 0
    try:
        amount = parse_amount(value)
    except ValueError as exc:
        raise InvalidRule("The threshold must be a positive number.") from exc
    if kind in PERCENT_KINDS:
        if not Decimal(0) < amount < Decimal(100):
            raise InvalidRule("The percentage must be greater than 0 and less than 100.")
        return int((amount * 100).to_integral_value())
    if amount <= 0:
        raise InvalidRule("The target price must be greater than zero.")
    return to_minor(amount, currency)


def describe(rule: AlertRule, currency: str) -> str:
    percent = f"{Decimal(rule.threshold) / 100:.2f}".rstrip("0").rstrip(".") + "%"
    if rule.kind == PCT_BELOW_BASELINE:
        return f"{percent} below baseline"
    if rule.kind == FIXED_TARGET:
        return f"At or below {format_money(rule.threshold, currency)}"
    if rule.kind == PCT_BELOW_AVERAGE:
        return f"{percent} below the {rule.lookback_days}-day average"
    if rule.kind == NEW_LOW:
        return "New historical low"
    if rule.kind == BACK_IN_STOCK:
        return "Back in stock"
    return f"Drops {percent} or more since the last check"


def create_rule(
    session: Session, product: Product, kind: str, threshold: object = None, **options: object
) -> AlertRule:
    rule = AlertRule(
        product_id=product.id, kind=kind, threshold=encode_threshold(kind, threshold, rule_currency(product))
    )
    session.add(rule)
    update_rule(rule, product, **options)
    return rule


def _bounded_int(name: str, value: object, low: int, high: int, *, optional: bool = False) -> int | None:
    if optional and value in (None, "", 0, "0"):
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise InvalidRule(f"{name} must be a whole number.") from exc
    if not low <= number <= high:
        raise InvalidRule(f"{name} must be between {low} and {high}.")
    return number


def update_rule(rule: AlertRule, product: Product, **values: object) -> None:
    if "threshold" in values and values["threshold"] is not None:
        rule.threshold = encode_threshold(rule.kind, values["threshold"], rule_currency(product))
        rule.is_below = False  # a new threshold starts from a clean state
    if "enabled" in values and values["enabled"] is not None:
        rule.enabled = bool(values["enabled"])
    if values.get("channels") is not None:
        channels = values["channels"]
        if not isinstance(channels, list) or not all(c in ("discord", "email", "mock") for c in channels):
            raise InvalidRule("channels must be a list containing discord, email or mock.")
        rule.channels = list(dict.fromkeys(channels))
    if values.get("cooldown_minutes") is not None:
        rule.cooldown_minutes = _bounded_int("cooldown_minutes", values["cooldown_minutes"], 0, 43200) or 0
    if values.get("lookback_days") is not None:
        rule.lookback_days = _bounded_int("lookback_days", values["lookback_days"], 1, 3650) or 30
    if "reminder_minutes" in values:
        rule.reminder_minutes = _bounded_int("reminder_minutes", values["reminder_minutes"], 10, 43200, optional=True)
    if "further_drop_percent" in values:
        raw = values["further_drop_percent"]
        if raw in (None, "", 0, "0"):
            rule.further_drop_bp = None
        else:
            try:
                percent = Decimal(str(raw))
            except InvalidOperation as exc:
                raise InvalidRule("further_drop_percent must be a number.") from exc
            if not percent.is_finite() or not Decimal(0) < percent < Decimal(100):
                raise InvalidRule("further_drop_percent must be greater than 0 and less than 100.")
            rule.further_drop_bp = int((percent * 100).to_integral_value())


@dataclass(frozen=True)
class Condition:
    below: bool
    target_minor: int | None
    reference_minor: int | None


def evaluate_condition(
    session: Session,
    rule: AlertRule,
    product: Product,
    observation: PriceObservation,
    previous_minor: int | None,
    previous_low_minor: int | None,
    now: datetime,
    previous_availability: str = "unknown",
) -> Condition | None:
    """Is the rule's condition met by this observation? None when it cannot be evaluated yet."""
    price = observation.price_minor
    if rule.kind == PCT_BELOW_BASELINE:
        if product.baseline_minor is None:
            return None
        target = apply_discount(product.baseline_minor, rule.threshold)
        return Condition(price <= target, target, product.baseline_minor)
    if rule.kind == FIXED_TARGET:
        return Condition(price <= rule.threshold, rule.threshold, product.baseline_minor)
    if rule.kind == PCT_BELOW_AVERAGE:
        since = now - timedelta(days=rule.lookback_days)
        average, count = average_minor(session, product.id, since=since, before_id=observation.id)
        if average is None or count < MIN_AVERAGE_OBSERVATIONS:
            return None  # not enough real history yet
        target = apply_discount(average, rule.threshold)
        return Condition(price <= target, target, average)
    if rule.kind == BACK_IN_STOCK:
        # Only a confirmed out-of-stock state followed by a valid in-stock price counts.
        returned = previous_availability == "out_of_stock" and observation.availability == "in_stock"
        return Condition(returned, None, product.baseline_minor)
    if rule.kind == NEW_LOW:
        if previous_low_minor is None:
            return None
        return Condition(price < previous_low_minor, previous_low_minor, previous_low_minor)
    if previous_minor is None:
        return None
    target = apply_discount(previous_minor, rule.threshold)
    return Condition(price <= target, target, previous_minor)


def decide(rule: AlertRule, below: bool, price_minor: int, now: datetime) -> tuple[str | None, bool]:
    """Pure state transition. Returns (event_type or None, new is_below state)."""
    if not below:
        return None, False  # above the threshold again: eligible for a fresh alert
    in_cooldown = rule.last_triggered_at is not None and now - rule.last_triggered_at < timedelta(
        minutes=rule.cooldown_minutes
    )
    if rule.kind not in STATE_KINDS:
        return (None if in_cooldown else "trigger"), False
    if not rule.is_below:
        # Crossing. During a cooldown the rule stays armed, so the alert fires once the cooldown ends.
        return (None, False) if in_cooldown else ("trigger", True)
    if in_cooldown or rule.last_triggered_at is None:
        return None, True
    if (
        rule.further_drop_bp
        and rule.last_trigger_price_minor
        and price_minor <= apply_discount(rule.last_trigger_price_minor, rule.further_drop_bp)
    ):
        return "further_drop", True
    if rule.reminder_minutes and now - rule.last_triggered_at >= timedelta(minutes=rule.reminder_minutes):
        return "reminder", True
    return None, True


def evaluate_rules(
    session: Session,
    product: Product,
    observation: PriceObservation,
    previous_minor: int | None,
    previous_low_minor: int | None,
    now: datetime,
    previous_availability: str = "unknown",
) -> list[tuple[AlertEvent, AlertRule]]:
    """Evaluate every enabled rule against one valid observation and persist the resulting events."""
    events: list[tuple[AlertEvent, AlertRule]] = []
    for rule in product.rules:
        if not rule.enabled:
            continue
        condition = evaluate_condition(
            session, rule, product, observation, previous_minor, previous_low_minor, now, previous_availability
        )
        if condition is None:
            continue
        event_type, rule.is_below = decide(rule, condition.below, observation.price_minor, now)
        log.debug(
            "alert rule evaluated",
            extra={"rule_id": rule.id, "below": condition.below, "event": event_type, "target": condition.target_minor},
        )
        if event_type is None:
            continue
        rule.last_triggered_at = now
        if event_type != "reminder":
            rule.last_trigger_price_minor = observation.price_minor
        event = AlertEvent(
            product_id=product.id,
            rule_id=rule.id,
            observation_id=observation.id,
            rule_kind=rule.kind,
            event_type=event_type,
            price_minor=observation.price_minor,
            reference_minor=condition.reference_minor,
            target_minor=condition.target_minor,
            currency=observation.currency,
            created_at=now,
        )
        session.add(event)
        events.append((event, rule))
        log.info("alert event created", extra={"product_id": product.id, "rule_id": rule.id, "type": event_type})
    session.flush()
    return events


def primary_target(product: Product) -> int | None:
    """The target price shown on cards and charts: the first enabled baseline or fixed rule."""
    for rule in product.rules:
        if not rule.enabled:
            continue
        if rule.kind == PCT_BELOW_BASELINE and product.baseline_minor is not None:
            return apply_discount(product.baseline_minor, rule.threshold)
        if rule.kind == FIXED_TARGET:
            return rule.threshold
    return None
