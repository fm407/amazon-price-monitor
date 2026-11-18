"""REST API under /api/v1. Every route except the health checks requires authentication."""

import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import __version__, alerts, budget, notify, products
from app.checker import check_product, provider_chain
from app.context import AppContext
from app.models import AlertEvent, AlertRule, NotificationDelivery, Product, ProviderRequest
from app.money import micro_to_usd, to_minor
from app.providers import provider_label
from app.providers.mock import MockProvider
from app.schemas import (
    AlertCreate,
    AlertOut,
    AlertUpdate,
    BaselineUpdate,
    DeliveryOut,
    Envelope,
    EventOut,
    HistoryOut,
    Meta,
    MockControl,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    ProviderRequestOut,
    alert_out,
    change_percent,
    delivery_out,
    event_out,
    observation_out,
    product_out,
    request_out,
)
from app.security import get_ctx, get_session, require_csrf, require_user

public = APIRouter(prefix="/api/v1", tags=["health"])
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user), Depends(require_csrf)])

Page = Query(1, ge=1, le=100000)
Limit = Query(50, ge=1, le=200)
SORTS = ("title", "discount", "price", "change", "added")


def ok(data: Any, total: int | None = None, page: int = 1, limit: int = 0) -> Envelope[Any]:
    meta = None if total is None else Meta(total=total, page=page, limit=limit)
    return Envelope(data=data, meta=meta)


def paginate(session: Session, query: Any, page: int, limit: int) -> tuple[list[Any], int]:
    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    return list(session.scalars(query.limit(limit).offset((page - 1) * limit))), total


def get_product(product_id: int, session: Session = Depends(get_session)) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    return product


def get_rule(alert_id: int, session: Session = Depends(get_session)) -> AlertRule:
    rule = session.get(AlertRule, alert_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found.")
    return rule


def list_products(session: Session, q: str = "", status: str = "", sort: str = "title") -> list[Product]:
    """Filtered, sorted product list shared by the API and the dashboard."""
    query = select(Product).options(selectinload(Product.rules))
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(Product.title.ilike(pattern) | Product.asin.ilike(pattern) | Product.brand.ilike(pattern))
    if status in ("active", "paused"):
        query = query.where(Product.status == status)
    elif status == "failing":
        query = query.where(Product.consecutive_failures > 0)
    elif status == "below":
        query = query.where(Product.rules.any(AlertRule.is_below & AlertRule.enabled))
    rows = list(session.scalars(query))
    # ponytail: sorted in Python; a personal watchlist is small. Move to SQL if it reaches thousands of rows.
    if sort == "discount":
        rows.sort(key=lambda p: (change_percent(p) is None, change_percent(p) or 0))
    elif sort == "price":
        rows.sort(key=lambda p: (p.latest_price_minor is None, p.latest_price_minor or 0))
    elif sort == "change":
        rows.sort(
            key=lambda p: (
                p.last_price_change_at is None,
                -(p.last_price_change_at.timestamp() if p.last_price_change_at else 0),
            )
        )
    elif sort == "added":
        rows.sort(key=lambda p: p.id, reverse=True)
    else:
        rows.sort(key=lambda p: p.title.lower())
    return rows


# --- Health (public, no secrets, no external calls) --------------------------------------------


def health_report(request: Request) -> dict[str, Any]:
    ctx: AppContext = request.app.state.ctx
    checks = {"database": False, "data_dir_writable": False, "scheduler": False}
    try:
        with ctx.sessions() as session:
            session.execute(select(1))
        checks["database"] = True
    except Exception:  # noqa: S110 - reported through the check result below
        pass
    checks["data_dir_writable"] = os.access(ctx.settings.data_dir, os.W_OK)
    scheduler = getattr(request.app.state, "scheduler", None)
    checks["scheduler"] = scheduler.healthy if scheduler else not ctx.settings.scheduler_enabled
    return {"status": "ok" if all(checks.values()) else "unhealthy", "version": __version__, "checks": checks}


@public.get("/health", summary="Readiness: database, data directory and scheduler")
def health(request: Request, response: Response) -> Envelope[dict[str, Any]]:
    report = health_report(request)
    if report["status"] != "ok":
        response.status_code = 503
    return Envelope(success=report["status"] == "ok", data=report)


@public.get("/health/live", summary="Liveness: the process can serve requests")
def live() -> Envelope[dict[str, str]]:
    return Envelope(data={"status": "alive"})


# --- Products ----------------------------------------------------------------------------------


@router.get("/products", response_model=Envelope[list[ProductOut]], tags=["products"])
def products_list(
    q: str = Query("", max_length=200),
    status: Literal["", "active", "paused", "failing", "below"] = "",
    sort: Literal["title", "discount", "price", "change", "added"] = "title",
    page: int = Page,
    limit: int = Limit,
    session: Session = Depends(get_session),
):
    rows = list_products(session, q, status, sort)
    start = (page - 1) * limit
    return ok([product_out(p) for p in rows[start : start + limit]], len(rows), page, limit)


@router.post("/products", response_model=Envelope[ProductOut], status_code=201, tags=["products"])
def products_create(body: ProductCreate, ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)):
    product = create_product(ctx, session, body)
    return ok(product_out(product))


def create_product(ctx: AppContext, session: Session, body: ProductCreate) -> Product:
    """Add a product with its initial alert rules. Shared by the API and the dashboard form."""
    options = body.model_dump(include={"check_interval_minutes", "provider", "seller_filter", "include_used"})
    product, _result = products.add_product(ctx, session, body.input, options)
    if body.discount_percent is not None:
        alerts.create_rule(session, product, "pct_below_baseline", body.discount_percent)
    if body.target_price is not None:
        alerts.create_rule(session, product, "fixed_target", body.target_price)
    session.commit()
    session.refresh(product)
    return product


@router.get("/products/{product_id}", response_model=Envelope[ProductOut], tags=["products"])
def products_get(product: Product = Depends(get_product)):
    return ok(product_out(product))


@router.patch("/products/{product_id}", response_model=Envelope[ProductOut], tags=["products"])
def products_update(
    body: ProductUpdate,
    product: Product = Depends(get_product),
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
):
    products.update_product(ctx, product, body.model_dump(exclude_unset=True))
    session.commit()
    return ok(product_out(product))


@router.delete("/products/{product_id}", response_model=Envelope[dict[str, int]], tags=["products"])
def products_delete(product: Product = Depends(get_product), session: Session = Depends(get_session)):
    product_id = product.id
    session.delete(product)
    session.commit()
    return ok({"deleted": product_id})


@router.post("/products/{product_id}/check", response_model=Envelope[dict[str, Any]], tags=["products"])
def products_check(product: Product = Depends(get_product), ctx: AppContext = Depends(get_ctx)):
    """Run a check now. Manual checks obey the same budget and rate limits as scheduled checks."""
    result = check_product(ctx, product.id)
    if result.status == "busy":
        raise HTTPException(status_code=409, detail=result.message)
    return ok(
        {"status": result.status, "category": result.category, "message": result.message, "provider": result.provider}
    )


@router.post("/products/{product_id}/pause", response_model=Envelope[ProductOut], tags=["products"])
def products_pause(
    product: Product = Depends(get_product), ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)
):
    products.set_status(ctx, product, "paused")
    session.commit()
    return ok(product_out(product))


@router.post("/products/{product_id}/resume", response_model=Envelope[ProductOut], tags=["products"])
def products_resume(
    product: Product = Depends(get_product), ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)
):
    products.set_status(ctx, product, "active")
    session.commit()
    return ok(product_out(product))


@router.post("/products/{product_id}/baseline", response_model=Envelope[ProductOut], tags=["products"])
def products_baseline(
    body: BaselineUpdate, product: Product = Depends(get_product), session: Session = Depends(get_session)
):
    """Set the baseline to a price, or reset it to the latest valid price when no price is given."""
    minor = None if body.price is None else to_minor(body.price, alerts.rule_currency(product))
    products.set_baseline(product, minor)
    session.commit()
    return ok(product_out(product))


@router.get("/products/{product_id}/history", response_model=Envelope[HistoryOut], tags=["products"])
def products_history(
    range: Literal["24h", "7d", "30d", "90d", "all"] = "all",  # noqa: A002 - public query parameter name
    page: int = Page,
    limit: int = Query(1000, ge=1, le=5000),
    product: Product = Depends(get_product),
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
):
    rows, total = products.history(session, product.id, ctx.now(), range, limit, (page - 1) * limit)
    summary = product_out(product)
    data = HistoryOut(
        currency=product.currency,
        baseline_price=summary.baseline_price,
        target_price=summary.target_price,
        observations=[observation_out(o) for o in rows],
    )
    return ok(data, total, page, limit)


def _export(rows: list[dict[str, Any]], fields: tuple[str, ...], fmt: str, name: str) -> Response:
    if fmt == "csv":
        headers = {"Content-Disposition": f'attachment; filename="{name}.csv"'}
        return Response(products.to_csv(rows, fields), media_type="text/csv", headers=headers)
    return Response(Envelope(data=rows).model_dump_json(), media_type="application/json")


@router.get("/products/{product_id}/history/export", tags=["export"])
def history_export(
    format: Literal["csv", "json"] = "csv",  # noqa: A002
    product: Product = Depends(get_product),
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
):
    rows, _ = products.history(session, product.id, ctx.now(), "all", limit=1_000_000)
    data = [products.history_export_row(o) for o in rows]
    return _export(data, products.HISTORY_EXPORT_FIELDS, format, f"history-{product.asin}")


@router.get("/export/products", tags=["export"])
def products_export(format: Literal["csv", "json"] = "csv", session: Session = Depends(get_session)):  # noqa: A002
    data = [products.product_export_row(p) for p in session.scalars(select(Product).order_by(Product.id))]
    return _export(data, products.PRODUCT_EXPORT_FIELDS, format, "products")


# --- Alerts and notifications ------------------------------------------------------------------


@router.get("/alerts", response_model=Envelope[list[AlertOut]], tags=["alerts"])
def alerts_list(
    product_id: int | None = None, page: int = Page, limit: int = Limit, session: Session = Depends(get_session)
):
    query = select(AlertRule).options(selectinload(AlertRule.product)).order_by(AlertRule.id)
    if product_id is not None:
        query = query.where(AlertRule.product_id == product_id)
    rows, total = paginate(session, query, page, limit)
    return ok([alert_out(r, alerts.rule_currency(r.product)) for r in rows], total, page, limit)


@router.post("/alerts", response_model=Envelope[AlertOut], status_code=201, tags=["alerts"])
def alerts_create(body: AlertCreate, session: Session = Depends(get_session)):
    product = get_product(body.product_id, session)
    options = body.model_dump(exclude={"product_id", "kind", "threshold"})
    rule = alerts.create_rule(session, product, body.kind, body.threshold, **options)
    session.commit()
    return ok(alert_out(rule, alerts.rule_currency(product)))


@router.patch("/alerts/{alert_id}", response_model=Envelope[AlertOut], tags=["alerts"])
def alerts_update(body: AlertUpdate, rule: AlertRule = Depends(get_rule), session: Session = Depends(get_session)):
    alerts.update_rule(rule, rule.product, **body.model_dump(exclude_unset=True))
    session.commit()
    return ok(alert_out(rule, alerts.rule_currency(rule.product)))


@router.delete("/alerts/{alert_id}", response_model=Envelope[dict[str, int]], tags=["alerts"])
def alerts_delete(rule: AlertRule = Depends(get_rule), session: Session = Depends(get_session)):
    rule_id = rule.id
    session.delete(rule)
    session.commit()
    return ok({"deleted": rule_id})


@router.get("/alert-events", response_model=Envelope[list[EventOut]], tags=["alerts"])
def events_list(
    product_id: int | None = None, page: int = Page, limit: int = Limit, session: Session = Depends(get_session)
):
    query = select(AlertEvent).order_by(AlertEvent.id.desc())
    if product_id is not None:
        query = query.where(AlertEvent.product_id == product_id)
    rows, total = paginate(session, query, page, limit)
    return ok([event_out(e) for e in rows], total, page, limit)


@router.get("/notifications", response_model=Envelope[list[DeliveryOut]], tags=["notifications"])
def notifications_list(page: int = Page, limit: int = Limit, session: Session = Depends(get_session)):
    rows, total = paginate(session, select(NotificationDelivery).order_by(NotificationDelivery.id.desc()), page, limit)
    return ok([delivery_out(d) for d in rows], total, page, limit)


@router.post("/notifications/test", response_model=Envelope[list[DeliveryOut]], tags=["notifications"])
def notifications_test(ctx: AppContext = Depends(get_ctx)):
    if not ctx.notifiers:
        raise HTTPException(status_code=409, detail="No notification channel is configured.")
    return ok([delivery_out(d) for d in notify.send_test(ctx)])


# --- Providers, usage, settings ----------------------------------------------------------------


def provider_status(ctx: AppContext, session: Session) -> list[dict[str, Any]]:
    """Health per provider, derived from local request history. No external call is made."""
    now = ctx.now()
    chain = provider_chain(ctx, None)
    result = []
    # Chain order first (the order in which providers are tried), then the remaining providers.
    ordered = chain + [name for name in ctx.providers if name not in chain]
    for name in ordered:
        provider = ctx.providers[name]
        last = session.scalar(
            select(ProviderRequest)
            .where(ProviderRequest.provider == name, ProviderRequest.category.not_like("skipped%"))
            .order_by(ProviderRequest.id.desc())
            .limit(1)
        )
        blocked = ctx.blocked_until.get(name)
        month_requests, month_micro = budget.provider_month(session, name, now)
        limits = []
        if provider.max_monthly_requests is not None:
            limits.append(f"{provider.max_monthly_requests} requests")
        if provider.max_monthly_spend_usd is not None:
            limits.append(f"${provider.max_monthly_spend_usd}")
        if not provider.configured:
            health = "not configured"
        elif blocked and blocked > now:
            health = "paused after errors"
        elif last is None:
            health = "no requests yet"
        else:
            health = "ok" if last.success else f"last request failed ({last.category})"
        result.append(
            {
                "name": name,
                "label": provider_label(name),
                "order": chain.index(name) + 1 if name in chain else None,
                "role": "primary" if chain[:1] == [name] else ("fallback" if name in chain else "available"),
                "configured": provider.configured,
                "paid": provider.is_paid,
                "estimated_cost_per_request_usd": str(provider.cost_per_request_usd),
                "health": health,
                "month_requests": month_requests,
                "month_cost_usd": str(micro_to_usd(month_micro)),
                "monthly_limit": " or ".join(limits) or None,
                "paused_until": blocked if blocked and blocked > now else None,
                "last_request_at": last.started_at if last else None,
            }
        )
    return result


def usage_report(ctx: AppContext, session: Session) -> dict[str, Any]:
    current = budget.usage(session, ctx.settings, ctx.now())
    since = budget.month_start(ctx.now())
    by_provider = session.execute(
        select(ProviderRequest.provider, func.count(), func.coalesce(func.sum(budget._BILLED), 0))
        .where(ProviderRequest.started_at >= since, budget._REAL)
        .group_by(ProviderRequest.provider)
    ).all()
    exhausted = None
    if not current.paid_allowed:
        exhausted = "paid_disabled"
    elif current.remaining_micro <= 0:
        exhausted = "monthly_budget"
    elif current.today_paid_requests >= current.daily_limit:
        exhausted = "daily_limit"
    return {
        "month": since.strftime("%Y-%m"),
        "requests": current.month_requests,
        "failed_requests": current.month_failed,
        "paid_requests": current.month_paid_requests,
        "paid_requests_today": current.today_paid_requests,
        "max_daily_api_requests": current.daily_limit,
        "cost_usd": str(micro_to_usd(current.month_cost_micro)),
        "cost_is_estimate": current.is_estimate or current.month_cost_micro == 0,
        "provider_reported_cost_usd": str(micro_to_usd(current.month_reported_micro)),
        "max_monthly_api_spend_usd": str(micro_to_usd(current.budget_micro)),
        "remaining_usd": str(micro_to_usd(current.remaining_micro)),
        "paid_requests_allowed": current.paid_allowed,
        "paid_requests_blocked_reason": exhausted,
        "by_provider": [
            {"provider": n, "requests": c, "cost_usd": str(micro_to_usd(int(m)))} for n, c, m in by_provider
        ],
    }


def settings_report(ctx: AppContext) -> dict[str, Any]:
    """Non-secret configuration only. Keys, tokens, passwords and webhook URLs are never included."""
    s = ctx.settings
    return {
        "version": __version__,
        "provider": s.provider,
        "fallback_provider": ", ".join(s.fallback_providers) or None,
        "provider_chain": [provider_label(name) for name in [s.provider, *s.fallback_providers]],
        "demo_mode": s.demo_mode,
        "timezone": s.timezone,
        "default_check_interval_minutes": s.default_check_interval_minutes,
        "allow_paid_api_requests": s.allow_paid_api_requests,
        "max_monthly_api_spend_usd": str(s.max_monthly_api_spend_usd),
        "max_daily_api_requests": s.max_daily_api_requests,
        "scheduler_enabled": s.scheduler_enabled,
        "discord": "configured" if s.discord_configured else "not configured",
        "smtp": "configured" if s.smtp_configured else "not configured",
        "notification_channels": list(ctx.notifiers),
        "api_token": "configured" if s.api_token else "not configured",
    }


@router.get("/providers", response_model=Envelope[list[dict[str, Any]]], tags=["system"])
def providers_get(ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)):
    return ok(provider_status(ctx, session))


@router.get("/usage", response_model=Envelope[dict[str, Any]], tags=["system"])
def usage_get(ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)):
    return ok(usage_report(ctx, session))


@router.get("/usage/requests", response_model=Envelope[list[ProviderRequestOut]], tags=["system"])
def usage_requests(
    failed_only: bool = False, page: int = Page, limit: int = Limit, session: Session = Depends(get_session)
):
    query = select(ProviderRequest).order_by(ProviderRequest.id.desc())
    if failed_only:
        query = query.where(ProviderRequest.success.is_(False))
    rows, total = paginate(session, query, page, limit)
    return ok([request_out(r) for r in rows], total, page, limit)


@router.get("/settings", response_model=Envelope[dict[str, Any]], tags=["system"])
def settings_get(ctx: AppContext = Depends(get_ctx)):
    return ok(settings_report(ctx))


@router.get("/openapi.json", include_in_schema=False)
def openapi(request: Request) -> dict[str, Any]:
    return request.app.openapi()


# --- Mock provider control (mock mode only) ----------------------------------------------------


def apply_mock_control(ctx: AppContext, asin: str, body: MockControl) -> None:
    provider = ctx.providers.get("mock")
    if not isinstance(provider, MockProvider):
        raise HTTPException(status_code=404, detail="The mock provider is not active.")
    fields: dict[str, Any] = {}
    if body.shipping_unknown:
        fields["shipping"] = None
    elif body.shipping is not None:
        fields["shipping"] = str(body.shipping)
    if body.seller is not None:
        fields["seller"] = body.seller
    if body.condition is not None:
        fields["condition"] = body.condition
    if body.price is not None:
        provider.set_price(asin, str(body.price), **fields)
    elif fields:
        provider.set_price(asin, provider.fetch_product(asin, "US").price, **fields)
    if body.mode is not None:
        provider.set_mode(body.mode, asin)


@router.post("/mock/products/{asin}", response_model=Envelope[dict[str, str]], tags=["mock"])
def mock_control(body: MockControl, asin: str, ctx: AppContext = Depends(get_ctx)):
    """Simulate a price or a failure mode for one ASIN. Available only when PROVIDER=mock."""
    apply_mock_control(ctx, asin.upper(), body)
    return ok({"asin": asin.upper()})
