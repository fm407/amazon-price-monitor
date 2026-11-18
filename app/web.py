"""Server-rendered dashboard. Routes stay thin and call the same services as the REST API."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import __version__, alerts, api, notify, products, security
from app.amazon import InvalidProductInput
from app.checker import check_product
from app.context import AppContext
from app.models import AlertEvent, AlertRule, NotificationDelivery, PriceObservation, Product, ProviderRequest
from app.money import format_money, to_minor
from app.providers import provider_label
from app.schemas import AlertCreate, AlertUpdate, MockControl, ProductCreate, ProductUpdate, change_percent
from app.security import get_ctx, get_session, require_csrf

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["money"] = format_money
templates.env.filters["provider"] = provider_label
INTERVAL_CHOICES = [
    (30, "30 minutes"),
    (60, "1 hour"),
    (180, "3 hours"),
    (360, "6 hours"),
    (720, "12 hours"),
    (1440, "24 hours"),
]
USER_ERRORS = (
    InvalidProductInput,
    products.DuplicateProduct,
    products.ProductNotFound,
    products.InvalidProductUpdate,
    alerts.InvalidRule,
)


class LoginRequired(Exception):
    pass


def require_web_user(request: Request, session: Session = Depends(get_session)) -> str:
    user = security.session_user(request, session)
    if not user:
        raise LoginRequired
    request.state.auth = "session"
    return user


public = APIRouter(include_in_schema=False, dependencies=[Depends(require_csrf)])
router = APIRouter(include_in_schema=False, dependencies=[Depends(require_web_user), Depends(require_csrf)])


def flash(request: Request, level: str, message: str) -> None:
    request.session["flash"] = [*request.session.get("flash", []), [level, message]]


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    ctx: AppContext = request.app.state.ctx
    tz = ctx.settings.tz

    def localtime(moment: datetime | None) -> str:
        if moment is None:
            return "never"
        local = moment.astimezone(tz)
        return f"{local:%b} {local.day}, {local.year}, {local.hour % 12 or 12}:{local:%M} {local:%p} {local:%Z}"

    base = {
        "csrf": security.csrf_token(request),
        "flashes": request.session.pop("flash", []),
        "user": request.session.get("user"),
        "mock_mode": ctx.settings.provider == "mock",
        "demo_mode": ctx.settings.demo_mode,
        "version": __version__,
        "localtime": localtime,
        "intervals": INTERVAL_CHOICES,
        "kind_labels": alerts.KIND_LABELS,
        "describe": alerts.describe,
        "default_provider": ctx.settings.provider,
    }
    return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)


def form_model[M: BaseModel](model: type[M], data: dict[str, Any]) -> M:
    """Validate form fields with the same schema the REST API uses. Empty fields mean 'not set'."""
    return model.model_validate({key: value for key, value in data.items() if value not in ("", None)})


def error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        return f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}"
    return str(exc)


def card(product: Product) -> dict[str, Any]:
    change = change_percent(product)
    latest, previous = product.latest_price_minor, product.previous_price_minor
    trend = "flat"
    if latest is not None and previous is not None and latest != previous:
        trend = "down" if latest < previous else "up"
    target, baseline, progress = alerts.primary_target(product), product.baseline_minor, None
    if target is not None and baseline is not None and latest is not None and baseline > target:
        progress = max(0, min(100, round((baseline - latest) * 100 / (baseline - target))))
    return {
        "p": product,
        "currency": alerts.rule_currency(product),
        "change": change,
        "trend": trend,
        "target": target,
        "progress": progress,
        "below": any(rule.is_below and rule.enabled for rule in product.rules),
    }


# --- Setup, login, logout ----------------------------------------------------------------------


@public.get("/setup")
def setup_form(request: Request, session: Session = Depends(get_session)):
    if security.admin_exists(session):
        return redirect("/login")
    return render(request, "setup.html")


@public.post("/setup")
async def setup_submit(request: Request, ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)):
    if security.admin_exists(session):
        return redirect("/login")
    form = await request.form()
    token, username = str(form.get("token", "")).strip(), str(form.get("username", "")).strip()
    password, confirm = str(form.get("password", "")), str(form.get("confirm", ""))
    throttle: security.LoginThrottle = request.app.state.throttle
    path = security.setup_token_path(ctx)
    expected = path.read_text().strip() if path.exists() else ""
    error = None
    if throttle.blocked():
        error = "Too many failed attempts. Wait 15 minutes and try again."
    elif not expected or not security.hmac.compare_digest(token.encode(), expected.encode()):
        throttle.record_failure()
        error = "The setup token is not correct. Find it in the container log."
    elif not 3 <= len(username) <= 60:
        error = "The username must be between 3 and 60 characters."
    else:
        error = security.validate_new_password(password, confirm)
    if error:
        return render(request, "setup.html", status_code=400, error=error, username=username)
    security.create_admin(ctx, session, username, password)
    security.log_in(request, username, session)
    flash(request, "success", "Administrator account created. You are signed in.")
    return redirect("/")


@public.get("/login")
def login_form(request: Request, session: Session = Depends(get_session)):
    if not security.admin_exists(session):
        return redirect("/setup")
    if security.session_user(request, session):
        return redirect("/")
    return render(request, "login.html")


@public.post("/login")
async def login_submit(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    username, password = str(form.get("username", "")).strip(), str(form.get("password", ""))
    throttle: security.LoginThrottle = request.app.state.throttle
    if throttle.blocked():
        return render(request, "login.html", status_code=429, error="Too many failed attempts. Wait 15 minutes.")
    stored_user = security.get_setting(session, security.ADMIN_USER_KEY) or ""
    stored_hash = security.get_setting(session, security.ADMIN_HASH_KEY) or ""
    # Always verify the hash, so response time does not reveal whether the username exists.
    password_ok = security.verify_password(password[: security.MAX_PASSWORD_LENGTH], stored_hash)
    if not (password_ok and security.hmac.compare_digest(username.encode(), stored_user.encode())):
        throttle.record_failure()
        security.log.warning("failed login attempt")
        return render(request, "login.html", status_code=401, error="Wrong username or password.", username=username)
    throttle.reset()
    security.log_in(request, username, session)
    return redirect("/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# --- Dashboard ---------------------------------------------------------------------------------


def overview(ctx: AppContext, session: Session) -> dict[str, Any]:
    count = lambda *where: session.scalar(select(func.count()).select_from(Product).where(*where)) or 0  # noqa: E731
    next_check = session.scalar(select(func.min(Product.next_check_at)).where(Product.status == "active"))
    drops = session.scalars(
        select(Product)
        .where(Product.last_price_change_at.is_not(None), Product.latest_price_minor < Product.previous_price_minor)
        .order_by(Product.last_price_change_at.desc())
        .limit(5)
    ).all()
    recent_events = session.scalars(
        select(AlertEvent).options(selectinload(AlertEvent.product)).order_by(AlertEvent.id.desc()).limit(5)
    ).all()
    failures = session.scalars(
        select(ProviderRequest).where(ProviderRequest.success.is_(False)).order_by(ProviderRequest.id.desc()).limit(5)
    ).all()
    return {
        "total": count(),
        "active": count(Product.status == "active"),
        "paused": count(Product.status == "paused"),
        "failing": count(Product.consecutive_failures > 0),
        "below": count(Product.rules.any(AlertRule.is_below & AlertRule.enabled)),
        "next_check": next_check,
        "usage": api.usage_report(ctx, session),
        "drops": drops,
        "events": recent_events,
        "failures": failures,
    }


@router.get("/")
def dashboard(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "title",
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
):
    sort = sort if sort in api.SORTS else "title"
    rows = api.list_products(session, q[:200], status, sort)
    return render(
        request,
        "dashboard.html",
        cards=[card(p) for p in rows],
        stats=overview(ctx, session),
        q=q,
        status=status,
        sort=sort,
        providers=[name for name, provider in ctx.providers.items() if provider.configured],
    )


@router.post("/products")
async def product_add(request: Request, ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)):
    form: dict[str, Any] = dict(await request.form())
    form.pop("csrf_token", None)
    try:
        product = api.create_product(ctx, session, form_model(ProductCreate, form))
    except products.DuplicateProduct as exc:
        flash(request, "warning", "This product is already tracked.")
        return redirect(f"/products/{exc.product_id}" if exc.product_id else "/")
    except (*USER_ERRORS, ValidationError) as exc:
        flash(request, "danger", f"Could not add the product. {error_text(exc)}")
        return redirect("/")
    if product.latest_price_minor is None:
        flash(request, "warning", f"Product saved, but no valid price yet: {product.last_error or 'unknown reason'}")
    else:
        flash(request, "success", f"Now tracking {product.title}.")
    return redirect(f"/products/{product.id}")


# --- Product details and actions ---------------------------------------------------------------


def web_product(product_id: int, session: Session = Depends(get_session)) -> Product:
    return api.get_product(product_id, session)


@router.get("/products/{product_id}")
def product_page(
    request: Request,
    product: Product = Depends(web_product),
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
):
    recent = lambda model, column: session.scalars(  # noqa: E731
        select(model).where(column == product.id).order_by(model.id.desc()).limit(15)
    ).all()
    deliveries = session.scalars(
        select(NotificationDelivery)
        .join(AlertEvent, NotificationDelivery.event_id == AlertEvent.id)
        .where(AlertEvent.product_id == product.id)
        .order_by(NotificationDelivery.id.desc())
        .limit(10)
    ).all()
    average, observation_count = alerts.average_minor(session, product.id)
    errors = [r for r in recent(ProviderRequest, ProviderRequest.product_id) if r.success is False][:8]
    return render(
        request,
        "product.html",
        c=card(product),
        p=product,
        average=average,
        observation_count=observation_count,
        observations=recent(PriceObservation, PriceObservation.product_id),
        deliveries=deliveries,
        errors=errors,
        providers=[name for name, provider in ctx.providers.items() if provider.configured],
        default_interval=ctx.settings.default_check_interval_minutes,
        channels=list(ctx.notifiers),
        min_average=alerts.MIN_AVERAGE_OBSERVATIONS,
    )


@router.post("/products/{product_id}/check")
def product_check(request: Request, product: Product = Depends(web_product), ctx: AppContext = Depends(get_ctx)):
    result = check_product(ctx, product.id)
    level = {"ok": "success", "busy": "warning", "skipped": "warning", "no_price": "warning"}.get(
        result.status, "danger"
    )
    flash(request, level, "Price check completed." if result.status == "ok" else f"Check result: {result.message}")
    return redirect(f"/products/{product.id}")


@router.post("/products/{product_id}/{action}")
async def product_action(
    request: Request,
    action: str,
    product: Product = Depends(web_product),
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
):
    form: dict[str, Any] = dict(await request.form())
    form.pop("csrf_token", None)
    back = f"/products/{product.id}"
    try:
        if action == "pause":
            products.set_status(ctx, product, "paused")
            flash(request, "success", "Monitoring paused.")
        elif action == "resume":
            products.set_status(ctx, product, "active")
            flash(request, "success", "Monitoring resumed.")
        elif action == "delete":
            session.delete(product)
            flash(request, "success", f"Deleted {product.title}.")
            back = "/"
        elif action == "baseline":
            raw = str(form.get("price", "")).strip()
            minor = to_minor(alerts.parse_amount(raw), alerts.rule_currency(product)) if raw else None
            products.set_baseline(product, minor)
            flash(request, "success", "Baseline updated.")
        elif action == "edit":
            form["include_used"] = "include_used" in form
            if form.get("check_interval_minutes") == "":
                product.check_interval_minutes = None
            if form.get("provider") == "":
                product.provider = None
            values = form_model(ProductUpdate, form).model_dump(exclude_unset=True)
            products.update_product(ctx, product, values)
            flash(request, "success", "Settings saved.")
        elif action == "rules":
            form["channels"] = [c for c in ctx.notifiers if f"channel_{c}" in form]
            body = form_model(
                AlertCreate,
                {**{k: v for k, v in form.items() if not k.startswith("channel_")}, "product_id": product.id},
            )
            alerts.create_rule(
                session,
                product,
                body.kind,
                body.threshold,
                **body.model_dump(exclude={"product_id", "kind", "threshold"}),
            )
            flash(request, "success", "Alert rule added.")
        elif action == "simulate":
            api.apply_mock_control(ctx, product.asin, form_model(MockControl, form))
            session.commit()
            return product_check(request, product, ctx)
        else:
            return render(request, "error.html", status_code=404, message="Unknown action.")
        session.commit()
    except (*USER_ERRORS, ValidationError, ValueError) as exc:
        session.rollback()
        flash(request, "danger", error_text(exc))
    return redirect(back)


@router.post("/rules/{alert_id}/{action}")
async def rule_action(
    request: Request,
    action: str,
    rule: AlertRule = Depends(api.get_rule),
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
):
    form: dict[str, Any] = dict(await request.form())
    back = f"/products/{rule.product_id}"
    try:
        if action == "delete":
            session.delete(rule)
            flash(request, "success", "Alert rule deleted.")
        elif action == "update":
            values: dict[str, Any] = {
                k: v for k, v in form.items() if k != "csrf_token" and not k.startswith("channel_")
            }
            values["enabled"] = "enabled" in form
            values["channels"] = [c for c in ctx.notifiers if f"channel_{c}" in form]
            body = form_model(AlertUpdate, values)
            changes = body.model_dump(exclude_unset=True)
            changes.setdefault("reminder_minutes", None)
            changes.setdefault("further_drop_percent", None)
            alerts.update_rule(rule, rule.product, **changes)
            flash(request, "success", "Alert rule saved.")
        else:
            return render(request, "error.html", status_code=404, message="Unknown action.")
        session.commit()
    except (*USER_ERRORS, ValidationError, ValueError) as exc:
        session.rollback()
        flash(request, "danger", error_text(exc))
    return redirect(back)


# --- Activity and settings ---------------------------------------------------------------------


@router.get("/activity")
def activity(request: Request, session: Session = Depends(get_session)):
    events = session.scalars(
        select(AlertEvent).options(selectinload(AlertEvent.product)).order_by(AlertEvent.id.desc()).limit(50)
    ).all()
    deliveries = session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id.desc()).limit(50)).all()
    failures = session.scalars(
        select(ProviderRequest).where(ProviderRequest.success.is_(False)).order_by(ProviderRequest.id.desc()).limit(50)
    ).all()
    return render(request, "activity.html", events=events, deliveries=deliveries, failures=failures)


@router.get("/settings")
def settings_page(request: Request, ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)):
    return render(
        request,
        "settings.html",
        info=api.settings_report(ctx),
        usage=api.usage_report(ctx, session),
        provider_rows=api.provider_status(ctx, session),
        health=api.health_report(request),
    )


@router.post("/settings/test-notification")
def test_notification(request: Request, ctx: AppContext = Depends(get_ctx)):
    results = notify.send_test(ctx) if ctx.notifiers else []
    return render(request, "_test_result.html", results=results)


def percent_text(value: Decimal | None) -> str:
    return "unknown" if value is None else f"{abs(value)}%"


def usd_text(value: object) -> str:
    """Dollars with two decimals, or four when the amount has sub-cent precision (API costs)."""
    amount = Decimal(str(value))
    return f"${amount:,.2f}" if amount == amount.quantize(Decimal("0.01")) else f"${amount:,.4f}"


templates.env.filters["pct"] = percent_text
templates.env.filters["usd"] = usd_text
