"""The price check pipeline: fetch (budget, retry, fallback) -> normalize -> record -> schedule."""

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app import alerts, budget, notify
from app.context import AppContext
from app.logging_config import job_id_var, redact
from app.models import PriceObservation, Product
from app.money import apply_discount, format_money
from app.normalize import NormalizedPrice, PriceRejected, normalize
from app.providers.base import ProductData, ProviderError

log = logging.getLogger(__name__)

MAX_INLINE_WAIT_SECONDS = 30.0
RATE_LIMIT_BLOCK_SECONDS = 60.0
FAILURE_BACKOFF_MINUTES = 15
NOT_FOUND_PAUSE_AFTER = 3
RAW_LIMIT_BYTES = 4000
# Failures of the provider itself. Only these justify trying the fallback provider.
FALLBACK_CATEGORIES = {
    "auth", "quota", "rate_limited", "timeout", "network", "http_error", "malformed",
    "not_configured", "run_failed", "unsupported_marketplace", "provider_blocked", "provider_quota",
    "provider_paced", "internal",
}  # fmt: skip
# Failures worth retrying sooner than the normal interval.
TRANSIENT_CATEGORIES = {
    "rate_limited",
    "timeout",
    "network",
    "http_error",
    "malformed",
    "run_failed",
    "internal",
}


@dataclass(frozen=True)
class CheckResult:
    status: str  # ok | no_price | error | skipped | busy | deleted
    category: str = "ok"
    message: str = ""
    provider: str | None = None
    observation_id: int | None = None


@dataclass(frozen=True)
class FetchFailure:
    category: str
    message: str
    skipped: bool = False


def provider_chain(
    ctx: AppContext, product_provider: str | None, seller_filter: str = "any", avoid_first: str | None = None
) -> list[str]:
    """Providers to try, in order. Providers that cannot serve this product's settings are left out."""
    primary = product_provider if product_provider in ctx.providers else ctx.settings.provider
    chain = [primary] if primary in ctx.providers else []
    for fallback in ctx.settings.fallback_providers:
        if fallback not in chain and fallback in ctx.providers:
            chain.append(fallback)
    if seller_filter == "amazon":
        # "Amazon as seller only" needs the seller's name. A provider that never reports it would
        # use up its allowance on prices that are always rejected.
        chain = [name for name in chain if ctx.providers[name].reports_seller]
    if avoid_first in chain and len(chain) > 1:
        # A second reading is worth more from a different source than the first one.
        chain = [name for name in chain if name != avoid_first] + [avoid_first]
    return chain


def _prefer(current: "FetchFailure | None", new: "FetchFailure") -> "FetchFailure":
    """Keep the most informative failure: a real provider error outranks a skip, which outranks 'not configured'."""

    def rank(failure: "FetchFailure") -> int:
        if failure.category == "not_configured":
            return 0
        return 1 if failure.skipped else 2

    return new if current is None or rank(new) >= rank(current) else current


def _retry_delay(ctx: AppContext, attempt: int, retry_after: float | None) -> float | None:
    """Seconds to wait before the next attempt, or None when the wait is too long to do inline."""
    if retry_after is not None:
        return retry_after if retry_after <= MAX_INLINE_WAIT_SECONDS else None
    base = float(2**attempt)
    return base + ctx.rng.uniform(0, base / 2)


def _attempt(ctx: AppContext, name: str, request_id: int, asin: str, marketplace: str) -> ProductData | ProviderError:
    provider = ctx.providers[name]
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        with ctx.provider_slot(name):
            data = provider.fetch_product(asin, marketplace)
    except ProviderError as err:
        budget.finalize(
            ctx, request_id, success=False, category=err.category, http_status=err.http_status,
            duration_ms=elapsed(), detail=err.message, actual_cost_usd=err.actual_cost_usd,
        )  # fmt: skip
        return err
    except Exception as exc:  # a provider bug must never take the scheduler down
        log.exception("provider raised an unexpected error", extra={"provider": name})
        detail = f"Unexpected provider error: {type(exc).__name__}"
        budget.finalize(
            ctx,
            request_id,
            success=False,
            category="internal",
            http_status=None,
            duration_ms=elapsed(),
            detail=detail,
        )
        return ProviderError("internal", detail)
    budget.finalize(
        ctx, request_id, success=True, category="ok", http_status=data.http_status,
        duration_ms=elapsed(), actual_cost_usd=data.actual_cost_usd,
    )  # fmt: skip
    return data


def fetch(
    ctx: AppContext, product_id: int | None, asin: str, marketplace: str, chain: list[str]
) -> tuple[ProductData | None, str | None, FetchFailure | None]:
    """Try each provider in the chain. Returns (data, provider_name, failure)."""
    failure: FetchFailure | None = None
    name: str | None = None
    for name in chain:
        provider = ctx.providers[name]
        if not provider.configured:
            # No request can be sent, so nothing is reserved against the budget or the provider's limit.
            failure = _prefer(
                failure, FetchFailure("not_configured", f"The {name} provider has no credentials configured.")
            )
            continue
        blocked = ctx.blocked_until.get(name)
        if blocked and blocked > ctx.now():
            budget.record_skip(ctx, provider, product_id, asin, "provider_blocked")
            failure = _prefer(
                failure, FetchFailure("provider_blocked", budget.DENIAL_MESSAGES["provider_blocked"], skipped=True)
            )
            continue
        for attempt in range(max(1, ctx.settings.provider_max_attempts)):
            request_id, denial = budget.reserve(ctx, provider, product_id, asin, attempt)
            if request_id is None:
                assert denial is not None  # noqa: S101
                if denial in budget.PROVIDER_LIMIT_DENIALS:
                    # This provider's own allowance is used up. The next provider in the chain may still have one.
                    failure = _prefer(failure, FetchFailure(denial, budget.DENIAL_MESSAGES[denial], skipped=True))
                    break
                # Global budget denials end the whole chain: never fall back to another paid provider.
                if denial == "monthly_budget":
                    budget.notify_exhausted_once(ctx)
                return None, name, FetchFailure(denial, budget.DENIAL_MESSAGES[denial], skipped=True)
            outcome = _attempt(ctx, name, request_id, asin, marketplace)
            if isinstance(outcome, ProductData):
                return outcome, name, None
            failure = FetchFailure(outcome.category, outcome.message)
            log.warning(
                "provider request failed",
                extra={"provider": name, "asin": asin, "category": outcome.category, "attempt": attempt + 1},
            )
            block = outcome.block_seconds
            delay = _retry_delay(ctx, attempt, outcome.retry_after) if outcome.retryable else None
            last_attempt = attempt + 1 >= ctx.settings.provider_max_attempts
            if outcome.category == "rate_limited" and (delay is None or last_attempt):
                block = max(block, outcome.retry_after or RATE_LIMIT_BLOCK_SECONDS)
            if block:
                ctx.blocked_until[name] = ctx.now() + timedelta(seconds=block)
            if delay is None or last_attempt:
                break
            ctx.sleep(delay)
        if failure is None or failure.category not in FALLBACK_CATEGORIES:
            break
    if failure is None:
        failure = FetchFailure("not_configured", "No configured price provider can check this product.")
    if name is not None and failure.category in budget.PROVIDER_LIMIT_DENIALS:
        # Every provider was at its own limit. One ledger entry per check records why it was skipped.
        budget.record_skip(ctx, ctx.providers[name], product_id, asin, failure.category)
    return None, name, failure


def schedule_next(ctx: AppContext, product: Product, now: datetime, *, transient_failure: bool = False) -> None:
    interval = product.check_interval_minutes or ctx.settings.default_check_interval_minutes
    delay = float(interval)
    if transient_failure:
        delay = min(delay, FAILURE_BACKOFF_MINUTES * 2.0 ** max(0, product.consecutive_failures - 1))
    jitter = ctx.rng.uniform(0, min(delay * 0.1, 10.0))
    product.next_check_at = now + timedelta(minutes=delay + jitter)


def placeholder_title(asin: str) -> str:
    return f"Amazon product {asin}"


def _trim_raw(data: ProductData) -> dict[str, Any]:
    raw: dict[str, Any] = {"provider_price_basis": data.price_basis, **data.raw}
    if len(json.dumps(raw, default=str)) > RAW_LIMIT_BYTES:
        return {"provider_price_basis": data.price_basis, "truncated": True}
    return json.loads(json.dumps(raw, default=str))


def _update_metadata(product: Product, data: ProductData) -> None:
    # Fill the title once. After that it belongs to the user, who may have renamed the product.
    if data.title and product.title == placeholder_title(product.asin):
        product.title = " ".join(data.title.split())[:500] or product.title
    if data.brand:
        product.brand = data.brand[:200]
    if data.image_url and data.image_url.startswith(("https://", "/static/")) and len(data.image_url) <= 1000:
        product.image_url = data.image_url
    product.availability = data.availability


def record_observation(
    session: Session,
    product: Product,
    price: NormalizedPrice,
    data: ProductData,
    provider: str,
    now: datetime,
) -> tuple[PriceObservation, int | None, int | None]:
    """Store a valid observation and update the product's price state.

    Returns (observation, previous_price, previous_lowest) for alert evaluation.
    """
    previous, previous_low = product.latest_price_minor, product.lowest_minor
    observation = PriceObservation(
        product_id=product.id,
        price_minor=price.monitored_minor,
        item_price_minor=price.item_minor,
        shipping_minor=price.shipping_minor,
        effective_minor=price.effective_minor,
        list_price_minor=price.list_minor,
        currency=price.currency,
        seller=data.seller[:200] if data.seller else None,
        condition=data.condition,
        availability=data.availability,
        provider=provider,
        price_basis=price.price_basis,
        coupon_text=data.coupon_text[:200] if data.coupon_text else None,
        raw=_trim_raw(data),
        observed_at=now,
    )
    session.add(observation)
    session.flush()
    current = price.monitored_minor
    product.currency = product.currency or price.currency
    if product.initial_price_minor is None:
        product.initial_price_minor = current
    if product.baseline_minor is None:
        product.baseline_minor = current  # the first valid price is the baseline
    if previous is not None and previous != current:
        product.previous_price_minor = previous
        product.last_price_change_at = now
        log.info("price changed", extra={"product_id": product.id, "old_minor": previous, "new_minor": current})
    product.latest_price_minor = current
    product.latest_seller = observation.seller
    product.latest_condition = observation.condition
    product.lowest_minor = current if previous_low is None else min(previous_low, current)
    product.highest_minor = current if product.highest_minor is None else max(product.highest_minor, current)
    product.last_success_at = now
    product.consecutive_failures = 0
    product.last_error = None
    return observation, previous, previous_low


def _hold_for_confirmation(
    ctx: AppContext, product: Product, price: NormalizedPrice, provider: str, now: datetime
) -> CheckResult | None:
    """Hold a sudden large drop until a second reading agrees. Returns a result when the price is held.

    One odd reading (a provider glitch, a different offer) must not send an alert. The first
    reading of a large drop is kept aside and a second check is scheduled soon. If the second
    reading is also a large drop, it is stored and evaluated as usual. If not, the first one is
    dropped.
    """
    threshold_bp = int(ctx.settings.confirm_drop_percent * 100)
    latest = product.latest_price_minor
    suspicious = bool(threshold_bp and latest and price.monitored_minor <= apply_discount(latest, threshold_bp))
    if suspicious and product.pending_price_minor is None:
        product.pending_price_minor = price.monitored_minor
        product.pending_since = now
        product.pending_provider = provider
        product.consecutive_failures = 0
        shown = format_money(price.monitored_minor, price.currency)
        message = f"A drop to {shown} is waiting for a second reading before it is recorded."
        product.last_error, product.last_error_at = message, now
        product.next_check_at = now + timedelta(minutes=ctx.settings.confirm_recheck_minutes)
        log.info("price drop held for confirmation", extra={"product_id": product.id, "minor": price.monitored_minor})
        return CheckResult("pending", "pending_confirmation", message, provider)
    if product.pending_price_minor is not None:
        log.info("held price drop resolved", extra={"product_id": product.id, "confirmed": suspicious})
    product.pending_price_minor = product.pending_since = product.pending_provider = None
    return None


def probe_provider(ctx: AppContext, name: str, asin: str, marketplace: str) -> dict[str, Any]:
    """Send one real request to one provider and report what it returned and how it would be used.

    Nothing is stored except the usual ledger entry. The request counts against the limits.
    """
    data, _name, failure = fetch(ctx, None, asin, marketplace, [name])
    if data is None:
        failure = failure or FetchFailure("internal", "Unknown failure")
        return {"ok": False, "provider": name, "category": failure.category, "message": redact(failure.message)}
    report: dict[str, Any] = {
        "ok": True,
        "provider": name,
        "asin": asin,
        "title": data.title,
        "brand": data.brand,
        "raw_price": None if data.price is None else str(data.price),
        "currency": data.currency,
        "list_price": None if data.list_price is None else str(data.list_price),
        "shipping": None if data.shipping is None else str(data.shipping),
        "seller": data.seller,
        "condition": data.condition,
        "availability": data.availability,
        "image_url": data.image_url,
        "reported_cost_usd": None if data.actual_cost_usd is None else str(data.actual_cost_usd),
    }
    try:
        price = normalize(data, expected_currency=None)
    except PriceRejected as rejected:
        report.update(usable=False, verdict=rejected.message)
    else:
        shown = format_money(price.monitored_minor, price.currency)
        report.update(usable=True, verdict=f"Would be recorded as {shown} ({price.price_basis}).")
    return report


def _apply_failure(ctx: AppContext, product: Product, failure: FetchFailure, now: datetime) -> CheckResult:
    product.last_error = redact(failure.message)[:500]
    product.last_error_at = now
    if failure.skipped:
        schedule_next(ctx, product, now)
        return CheckResult("skipped", failure.category, failure.message)
    product.consecutive_failures += 1
    if failure.category == "not_found" and product.consecutive_failures >= NOT_FOUND_PAUSE_AFTER:
        product.status = "paused"
        product.last_error = "Paused: the provider repeatedly reported that this product does not exist."
        log.warning("product paused after repeated not-found responses", extra={"product_id": product.id})
    schedule_next(ctx, product, now, transient_failure=failure.category in TRANSIENT_CATEGORIES)
    return CheckResult("error", failure.category, failure.message)


def _apply(
    ctx: AppContext,
    session: Session,
    product: Product,
    data: ProductData | None,
    provider: str | None,
    failure: FetchFailure | None,
) -> CheckResult:
    now = ctx.now()
    product.last_attempt_at = now
    if data is None or provider is None:
        return _apply_failure(ctx, product, failure or FetchFailure("internal", "Unknown failure"), now)
    previous_availability = product.availability
    _update_metadata(product, data)
    try:
        price = normalize(
            data,
            expected_currency=product.currency,
            seller_filter=product.seller_filter,
            include_used=product.include_used,
        )
    except PriceRejected as rejected:
        # The provider answered, but there is no valid price. Keep the last known valid price untouched.
        product.last_error = rejected.message
        product.last_error_at = now
        product.consecutive_failures = 0
        schedule_next(ctx, product, now)
        log.info("no valid price", extra={"product_id": product.id, "reason": rejected.code})
        return CheckResult("no_price", rejected.code, rejected.message, provider)
    held = _hold_for_confirmation(ctx, product, price, provider, now)
    if held is not None:
        return held
    observation, previous, previous_low = record_observation(session, product, price, data, provider, now)
    # Events and their deliveries are committed together with the observation, before anything is sent.
    events = alerts.evaluate_rules(session, product, observation, previous, previous_low, now, previous_availability)
    for event, rule in events:
        notify.queue_alert(session, ctx, event, rule, product, observation)
    schedule_next(ctx, product, now)
    return CheckResult("ok", "ok", "", provider, observation.id)


def check_product(ctx: AppContext, product_id: int) -> CheckResult:
    """Run one check. Safe to call from the scheduler and from manual requests concurrently."""
    if not ctx.try_begin_check(product_id):
        return CheckResult("busy", "busy", "A check for this product is already running.")
    token = job_id_var.set(f"check-{product_id}-{uuid.uuid4().hex[:8]}")
    try:
        with ctx.sessions() as session:
            product = session.get(Product, product_id)
            if product is None:
                return CheckResult("deleted", "deleted", "The product no longer exists.")
            asin, marketplace = product.asin, product.marketplace
            chain = provider_chain(ctx, product.provider, product.seller_filter, product.pending_provider)
        # No database session is held open during network I/O.
        data, provider, failure = fetch(ctx, product_id, asin, marketplace, chain)
        with ctx.sessions() as session:
            product = session.get(Product, product_id)
            if product is None:  # deleted while the request was in flight
                return CheckResult("deleted", "deleted", "The product was deleted during the check.")
            result = _apply(ctx, session, product, data, provider, failure)
            session.commit()
        if result.status == "ok":
            notify.deliver_pending(ctx)
        log.info(
            "product check finished",
            extra={"product_id": product_id, "status": result.status, "category": result.category},
        )
        return result
    except Exception:
        log.exception("product check crashed", extra={"product_id": product_id})
        return CheckResult("error", "internal", "The check failed unexpectedly. See the application log.")
    finally:
        ctx.end_check(product_id)
        job_id_var.reset(token)
