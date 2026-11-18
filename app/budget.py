"""API usage accounting and the hard spending guard.

Every provider call reserves its estimated cost in the database before any
network I/O. One process-wide lock makes check-and-reserve atomic, so concurrent
checks cannot overspend. The ledger is persistent, so limits survive restarts.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from app import notify
from app.config import Settings
from app.context import AppContext
from app.logging_config import redact
from app.models import ApplicationSetting, ProviderRequest
from app.money import micro_to_usd, usd_to_micro
from app.providers.base import PriceProvider

log = logging.getLogger(__name__)

DENIAL_MESSAGES = {
    "paid_disabled": "Paid API requests are disabled (ALLOW_PAID_API_REQUESTS=false).",
    "monthly_budget": "The monthly API budget is exhausted.",
    "daily_limit": "The daily API request limit is reached.",
    "provider_quota": "This provider's own monthly limit is reached.",
    "provider_blocked": "The provider is temporarily paused after an authentication, quota or rate-limit error.",
}
_BILLED = func.coalesce(ProviderRequest.actual_cost_micro, ProviderRequest.estimated_cost_micro)
_REAL = ProviderRequest.category.not_like("skipped%")


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Usage:
    month_requests: int
    month_failed: int
    month_paid_requests: int
    month_cost_micro: int  # reported cost where the provider reports it, estimate otherwise
    month_reported_micro: int  # the part of month_cost_micro that the provider reported
    today_paid_requests: int
    budget_micro: int
    daily_limit: int
    paid_allowed: bool

    @property
    def remaining_micro(self) -> int:
        return max(0, self.budget_micro - self.month_cost_micro)

    @property
    def is_estimate(self) -> bool:
        return self.month_reported_micro < self.month_cost_micro


def usage(session: Session, settings: Settings, now: datetime) -> Usage:
    since = month_start(now)
    month = (ProviderRequest.started_at >= since) & _REAL
    paid = month & ProviderRequest.is_paid
    row = session.execute(
        select(
            func.count().filter(month),
            func.count().filter(month & (ProviderRequest.success.is_(False))),
            func.count().filter(paid),
            func.coalesce(func.sum(_BILLED).filter(paid), 0),
            func.coalesce(func.sum(ProviderRequest.actual_cost_micro).filter(paid), 0),
            func.count().filter(paid & (ProviderRequest.started_at >= day_start(now))),
        )
    ).one()
    return Usage(
        month_requests=row[0],
        month_failed=row[1],
        month_paid_requests=row[2],
        month_cost_micro=int(row[3] or 0),
        month_reported_micro=int(row[4] or 0),
        today_paid_requests=row[5],
        budget_micro=usd_to_micro(settings.max_monthly_api_spend_usd),
        daily_limit=settings.max_daily_api_requests,
        paid_allowed=settings.allow_paid_api_requests,
    )


def _denial(current: Usage, cost_micro: int) -> str | None:
    if not current.paid_allowed:
        return "paid_disabled"
    if current.month_cost_micro + cost_micro > current.budget_micro:
        return "monthly_budget"
    if current.today_paid_requests >= current.daily_limit:
        return "daily_limit"
    return None


def provider_month(session: Session, name: str, now: datetime) -> tuple[int, int]:
    """(requests, billed micro-dollars) for one provider in the current month."""
    where = (ProviderRequest.provider == name) & (ProviderRequest.started_at >= month_start(now)) & _REAL
    count, billed = session.execute(select(func.count(), func.coalesce(func.sum(_BILLED), 0)).where(where)).one()
    return count, int(billed or 0)


def _provider_denial(session: Session, provider: PriceProvider, now: datetime, cost_micro: int) -> str | None:
    """A provider's own monthly limit. Unlike the global limits, it lets the next provider take over."""
    if provider.max_monthly_requests is None and provider.max_monthly_spend_usd is None:
        return None
    count, billed = provider_month(session, provider.name, now)
    if provider.max_monthly_requests is not None and count >= provider.max_monthly_requests:
        return "provider_quota"
    if provider.max_monthly_spend_usd is not None and billed + cost_micro > usd_to_micro(
        provider.max_monthly_spend_usd
    ):
        return "provider_quota"
    return None


def record_skip(ctx: AppContext, provider: PriceProvider, product_id: int | None, asin: str, reason: str) -> None:
    with ctx.sessions() as session:
        session.add(
            ProviderRequest(
                provider=provider.name,
                product_id=product_id,
                asin=asin,
                started_at=ctx.now(),
                success=False,
                category=f"skipped_{reason}",
                detail=DENIAL_MESSAGES[reason],
            )
        )
        session.commit()
    log.warning("provider request skipped", extra={"provider": provider.name, "asin": asin, "reason": reason})


def reserve(
    ctx: AppContext, provider: PriceProvider, product_id: int | None, asin: str, retry_count: int
) -> tuple[int | None, str | None]:
    """Reserve one request. Returns (request_id, None) or (None, denial_reason)."""
    cost_micro = usd_to_micro(provider.cost_per_request_usd) if provider.is_paid else 0
    with ctx.budget_lock, ctx.sessions() as session:
        reason = _denial(usage(session, ctx.settings, ctx.now()), cost_micro) if provider.is_paid else None
        if reason is None:
            reason = _provider_denial(session, provider, ctx.now(), cost_micro)
        if reason is None:
            request = ProviderRequest(
                provider=provider.name,
                product_id=product_id,
                asin=asin,
                started_at=ctx.now(),
                is_paid=provider.is_paid,
                estimated_cost_micro=cost_micro,
                retry_count=retry_count,
            )
            session.add(request)
            session.commit()
            return request.id, None
    record_skip(ctx, provider, product_id, asin, reason)
    return None, reason


def finalize(
    ctx: AppContext,
    request_id: int,
    *,
    success: bool,
    category: str,
    http_status: int | None,
    duration_ms: int,
    detail: str | None = None,
    actual_cost_usd: Decimal | None = None,
) -> None:
    values: dict[str, Any] = {
        "success": success,
        "category": category,
        "http_status": http_status,
        "duration_ms": duration_ms,
        "detail": redact(detail)[:300] if detail else None,  # provider text may echo a credential
    }
    if actual_cost_usd is not None:
        values["actual_cost_micro"] = usd_to_micro(actual_cost_usd)
    with ctx.sessions() as session:
        session.execute(update(ProviderRequest).where(ProviderRequest.id == request_id).values(**values))
        session.commit()


def recover_interrupted(session: Session) -> int:
    """After a restart, close requests that never finished. Their reserved cost stays counted."""
    result = session.execute(
        update(ProviderRequest)
        .where(ProviderRequest.success.is_(None))
        .values(success=False, category="interrupted", detail="The application restarted during this request.")
    )
    return cast(CursorResult[Any], result).rowcount or 0


BUDGET_NOTIFIED_KEY = "budget_notified_month"


def notify_exhausted_once(ctx: AppContext) -> bool:
    """Send one notification per calendar month when the monthly budget stops paid requests."""
    month = ctx.now().strftime("%Y-%m")
    with ctx.budget_lock, ctx.sessions() as session:
        row = session.get(ApplicationSetting, BUDGET_NOTIFIED_KEY)
        if row is not None and row.value == month:
            return False
        if row is None:
            session.add(ApplicationSetting(key=BUDGET_NOTIFIED_KEY, value=month))
        else:
            row.value = month
        current = usage(session, ctx.settings, ctx.now())
        body = (
            "API BUDGET EXHAUSTED\n\n"
            f"Paid price checks are stopped for {month}.\n"
            f"Spend this month: ${micro_to_usd(current.month_cost_micro)}"
            f"{' (estimate)' if current.is_estimate else ''}\n"
            f"Monthly limit: ${micro_to_usd(current.budget_micro)}\n"
            "The application stays online. Checks resume next month or after you raise "
            "MAX_MONTHLY_API_SPEND_USD."
        )
        notify.queue(session, ctx, subject="Amazon Price Monitor: API budget exhausted", body=body)
        session.commit()
    log.warning("monthly API budget exhausted", extra={"month": month})
    notify.deliver_pending(ctx)
    return True
