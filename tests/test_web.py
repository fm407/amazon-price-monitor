"""Dashboard pages and forms, demo data, and output escaping."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import demo, security
from app.config import Settings
from app.main import create_app
from app.models import AlertEvent, AlertRule, NotificationDelivery, PriceObservation, Product
from tests.conftest import GPU, make_ctx

PASSWORD = "correct horse battery"


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def sign_in(ctx) -> TestClient:
    client = TestClient(create_app(ctx=ctx), follow_redirects=True)
    client.__enter__()
    token = security.setup_token_path(ctx).read_text()
    csrf = csrf_from(client.get("/setup").text)
    form = {"csrf_token": csrf, "token": token, "username": "admin", "password": PASSWORD, "confirm": PASSWORD}
    assert client.post("/setup", data=form).status_code == 200
    return client


@pytest.fixture
def web(ctx):
    client = sign_in(ctx)
    yield client
    client.__exit__(None, None, None)


def post(web, url, **data):
    return web.post(url, data={"csrf_token": csrf_from(web.get("/").text), **data})


def product(ctx) -> Product:
    with ctx.sessions() as session:
        return session.scalars(select(Product)).one()


def test_add_product_form_creates_product_rule_and_first_observation(web, ctx):
    page = post(
        web,
        "/products",
        input="https://www.amazon.com/dp/B0DEMOGPU1?tag=x",
        discount_percent="25",
        target_price="",
        check_interval_minutes="",
    )
    assert page.status_code == 200 and "Now tracking ASRock Intel Arc Pro B60" in page.text
    assert "$800.00" in page.text and "25% below baseline" in page.text and "$600.00" in page.text
    again = post(web, "/products", input=GPU, discount_percent="25")
    assert "already tracked" in again.text
    invalid = post(web, "/products", input="https://example.com/dp/B0DEMOGPU1")
    assert "Could not add the product" in invalid.text and "not a recognised Amazon product link" in invalid.text
    with ctx.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 1


def test_pages_render_with_expected_content(web, ctx, mock):
    post(web, "/products", input=GPU, discount_percent="25")
    pid = product(ctx).id
    mock.set_price(GPU, "599.00")
    checked = post(web, f"/products/{pid}/check")
    assert "Price check completed." in checked.text and "Below target" in checked.text
    dashboard = web.get("/")
    for text in (
        "Products tracked",
        "Below alert threshold",
        "API requests this month",
        "Recent price drops",
        "100% to target $600.00",
        "Mock provider active",
    ):
        assert text in dashboard.text, text
    assert "&#9660; down" in dashboard.text  # the trend has a symbol and a word, not colour alone
    detail = web.get(f"/products/{pid}")
    for text in (
        "Price history",
        "Alert rules",
        "Recent price observations",
        "Historical low",
        "Simulate a mock price",
        "price-chart",
    ):
        assert text in detail.text, text
    activity = web.get("/activity")
    assert "Percent below baseline" in activity.text and "sent" in activity.text
    settings = web.get("/settings")
    for text in ("Price providers", "Send test notification", "America/New_York", "Monthly limit", "Disabled"):
        assert text in settings.text, text
    assert web.get("/?q=nothing-matches").text.count("No products match this filter.") == 1
    assert web.get("/?status=below&sort=discount").status_code == 200
    assert web.get("/products/9999").status_code == 404


def test_product_actions_from_the_dashboard(web, ctx, mock):
    post(web, "/products", input=GPU, discount_percent="25")
    pid = product(ctx).id
    assert "Monitoring paused." in post(web, f"/products/{pid}/pause").text
    assert product(ctx).status == "paused"
    assert "Monitoring resumed." in post(web, f"/products/{pid}/resume").text
    post(
        web,
        f"/products/{pid}/edit",
        title="My card",
        check_interval_minutes="60",
        provider="",
        seller_filter="amazon",
        include_used="on",
    )
    stored = product(ctx)
    assert (stored.title, stored.check_interval_minutes, stored.seller_filter, stored.include_used) == (
        "My card",
        60,
        "amazon",
        True,
    )
    post(web, f"/products/{pid}/edit", title="My card", check_interval_minutes="", provider="", seller_filter="any")
    assert (product(ctx).check_interval_minutes, product(ctx).include_used) == (None, False)
    assert "Baseline updated." in post(web, f"/products/{pid}/baseline", price="700").text
    assert product(ctx).baseline_minor == 70000
    assert "not a number" in post(web, f"/products/{pid}/baseline", price="abc").text
    simulated = post(web, f"/products/{pid}/simulate", price="500.00", mode="ok")
    assert "Price check completed." in simulated.text and product(ctx).latest_price_minor == 50000
    outage = post(web, f"/products/{pid}/simulate", price="", mode="outage")
    assert "Simulated provider outage" in outage.text and product(ctx).latest_price_minor == 50000
    assert post(web, f"/products/{pid}/bogus").status_code == 404
    assert "Deleted My card." in post(web, f"/products/{pid}/delete").text
    with ctx.sessions() as session:
        for model in (Product, AlertRule, PriceObservation, AlertEvent, NotificationDelivery):
            assert session.scalar(select(func.count()).select_from(model)) == 0, model.__name__


def test_rule_forms(web, ctx):
    post(web, "/products", input=GPU, discount_percent="")
    pid = product(ctx).id
    assert (
        "Alert rule added."
        in post(web, f"/products/{pid}/rules", kind="fixed_target", threshold="550", cooldown_minutes="30").text
    )
    assert (
        "The threshold must be a positive number."
        in post(web, f"/products/{pid}/rules", kind="pct_below_baseline", threshold="").text
    )
    with ctx.sessions() as session:
        rule = session.scalars(select(AlertRule)).one()
    assert (rule.kind, rule.threshold, rule.cooldown_minutes) == ("fixed_target", 55000, 30)
    saved = post(
        web,
        f"/rules/{rule.id}/update",
        threshold="500",
        cooldown_minutes="0",
        reminder_minutes="720",
        further_drop_percent="5",
        enabled="on",
        channel_mock="on",
    )
    assert "Alert rule saved." in saved.text
    with ctx.sessions() as session:
        rule = session.get(AlertRule, rule.id)
    assert (rule.threshold, rule.reminder_minutes, rule.further_drop_bp, rule.enabled, rule.channels) == (
        50000,
        720,
        500,
        True,
        ["mock"],
    )
    post(
        web,
        f"/rules/{rule.id}/update",
        threshold="500",
        cooldown_minutes="0",
        reminder_minutes="",
        further_drop_percent="",
    )
    with ctx.sessions() as session:
        rule = session.get(AlertRule, rule.id)
    assert (rule.reminder_minutes, rule.further_drop_bp, rule.enabled, rule.channels) == (None, None, False, [])
    assert "Alert rule deleted." in post(web, f"/rules/{rule.id}/delete").text


def test_forms_reject_missing_csrf_token(web, ctx):
    post(web, "/products", input=GPU)
    pid = product(ctx).id
    assert web.post(f"/products/{pid}/delete", data={}).status_code == 403
    assert web.post(f"/products/{pid}/delete", data={"csrf_token": "forged"}).status_code == 403
    assert product(ctx).id == pid


def test_untrusted_product_text_is_escaped(web, ctx):
    post(web, "/products", input=GPU)
    payload = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    with ctx.sessions() as session:
        stored = session.scalars(select(Product)).one()
        stored.title, stored.brand, stored.last_error, stored.latest_seller = payload, payload, payload, payload
        session.commit()
        pid = stored.id
    for url in ("/", f"/products/{pid}", "/activity"):
        html = web.get(url).text
        assert "<script>alert" not in html and "<img src=x" not in html, url
    assert "&lt;script&gt;alert" in web.get("/").text


def test_test_notification_button_returns_a_fragment(web):
    fragment = post(web, "/settings/test-notification")
    assert "test message delivered" in fragment.text and "<html" not in fragment.text


def test_demo_mode_seeds_labelled_products_and_can_be_switched_off(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, scheduler_enabled=False, demo_mode=True, log_level="WARNING")
    ctx = make_ctx(settings)
    client = sign_in(ctx)
    with ctx.sessions() as session:
        rows = session.scalars(select(Product).order_by(Product.asin)).all()
        assert [p.asin for p in rows] == ["B0DEMOGPU1", "B0DEMORAM1", "B0DEMOSSD1"]
        assert all(p.is_demo and "[DEMO]" in p.title for p in rows)
        assert session.scalar(select(func.count()).select_from(PriceObservation)) == 36
        assert session.scalar(select(func.count()).select_from(AlertEvent)) >= 3
        assert (
            session.scalar(
                select(func.count()).select_from(NotificationDelivery).where(NotificationDelivery.status != "sent")
            )
            == 0
        )
        gpu = next(p for p in rows if p.asin == GPU)
        assert (gpu.baseline_minor, gpu.latest_price_minor) == (80000, 59900)
        assert any(rule.is_below for rule in gpu.rules)
    page = client.get("/").text
    assert "fictional demonstration data" in page and page.count(">Demo<") == 3
    demo.seed(ctx)  # idempotent
    with ctx.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 3
    assert ctx.providers["mock"].fetch_product(GPU, "US").price == "599.00"  # mock continues from stored prices
    client.__exit__(None, None, None)
    ctx.close()

    off = make_ctx(Settings(_env_file=None, data_dir=tmp_path, scheduler_enabled=False, log_level="WARNING"))
    demo.sync(off)
    with off.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 0
        assert session.scalar(select(func.count()).select_from(PriceObservation)) == 0
    off.close()


def test_demo_mode_requires_the_mock_provider():
    with pytest.raises(ValueError, match="DEMO_MODE"):
        Settings(_env_file=None, demo_mode=True, provider="canopy")
