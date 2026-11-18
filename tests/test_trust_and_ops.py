"""Provider test action, confirmation of large drops, pacing, back-in-stock alerts, scheduled backups."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import alerts, backup, budget, products
from app.checker import check_product, probe_provider, provider_chain
from app.config import Settings
from app.main import create_app
from app.models import AlertEvent, PriceObservation, Product, ProviderRequest
from app.scheduler import Scheduler
from tests.conftest import GPU, make_ctx
from tests.test_apify import paid_ctx, tracked
from tests.test_providers import CANOPY_OK
from tests.test_quotas import both_ok

TOKEN = "trust-and-ops-token-0123456789abcdef"


def count(ctx, model) -> int:
    with ctx.sessions() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def add(ctx, rule_kind=None, threshold=None, **options) -> Product:
    with ctx.sessions() as session:
        product, _ = products.add_product(ctx, session, GPU)
        if rule_kind:
            alerts.create_rule(session, product, rule_kind, threshold, **options)
            session.commit()
        return product


def reload(ctx, product) -> Product:
    with ctx.sessions() as session:
        return session.get(Product, product.id)


# --- Test a provider ---------------------------------------------------------------------------


def test_probe_reports_mapped_fields_and_stores_nothing_but_a_ledger_entry(ctx, mock):
    report = probe_provider(ctx, "mock", GPU, "US")
    assert report["ok"] and report["usable"]
    assert (report["raw_price"], report["currency"], report["seller"], report["availability"]) == (
        "800.00",
        "USD",
        "Amazon.com",
        "in_stock",
    )
    assert report["verdict"] == "Would be recorded as $800.00 (delivered)."
    assert (count(ctx, Product), count(ctx, PriceObservation), count(ctx, ProviderRequest)) == (0, 0, 1)
    mock.set_mode("no_price", GPU)
    rejected = probe_provider(ctx, "mock", GPU, "US")
    assert rejected["ok"] and rejected["usable"] is False and rejected["verdict"] == "The provider returned no price."
    mock.set_mode("outage", GPU)
    failed = probe_provider(ctx, "mock", GPU, "US")
    assert (failed["ok"], failed["category"]) == (False, "network")


def test_probe_obeys_limits_and_never_calls_a_denied_provider(tmp_path):
    calls: list[str] = []
    ctx = paid_ctx(tmp_path, both_ok(calls), canopy_max_monthly_requests=1, fallback_provider="")
    assert probe_provider(ctx, "canopy", "B0EXAMPLE1", "US")["ok"]
    denied = probe_provider(ctx, "canopy", "B0EXAMPLE1", "US")
    assert (denied["ok"], denied["category"]) == (False, "provider_quota") and calls == ["canopy"]
    ctx.close()


def test_provider_test_endpoint_and_settings_form(ctx, mock):
    ctx.settings.api_token = TOKEN
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(ctx=ctx)) as client:
        ok = client.post(
            "/api/v1/providers/mock/test", json={"input": f"https://www.amazon.com/dp/{GPU}?tag=x"}, headers=auth
        )
        assert ok.status_code == 200 and ok.json()["data"]["usable"] is True and ok.json()["data"]["asin"] == GPU
        assert client.post("/api/v1/providers/nope/test", json={"input": GPU}, headers=auth).status_code == 404
        assert (
            client.post(
                "/api/v1/providers/mock/test", json={"input": "https://example.com/dp/B0DEMOGPU1"}, headers=auth
            ).status_code
            == 422
        )
        assert client.post("/api/v1/providers/mock/test", json={"input": GPU}).status_code == 401
    assert count(ctx, Product) == 0


# --- Confirmation of large drops ---------------------------------------------------------------


@pytest.fixture
def confirming(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        scheduler_enabled=False,
        log_level="WARNING",
        confirm_drop_percent=Decimal("15"),
    )
    context = make_ctx(settings)
    yield context
    context.close()


def test_large_drop_is_held_until_a_second_reading_agrees(confirming):
    ctx, mock = confirming, confirming.providers["mock"]
    product = add(ctx, "pct_below_baseline", "25")
    ctx.now.advance(hours=6)
    mock.set_price(GPU, "599.00")
    held = check_product(ctx, product.id)
    assert (held.status, held.category) == ("pending", "pending_confirmation")
    stored = reload(ctx, product)
    assert (stored.latest_price_minor, stored.pending_price_minor, stored.pending_provider) == (80000, 59900, "mock")
    assert (
        stored.next_check_at == ctx.now() + timedelta(minutes=15)
        and "waiting for a second reading" in stored.last_error
    )
    assert (count(ctx, PriceObservation), count(ctx, AlertEvent)) == (1, 0) and ctx.notifiers["mock"].sent == []

    ctx.now.advance(minutes=15)
    confirmed = check_product(ctx, product.id)
    assert confirmed.status == "ok"
    stored = reload(ctx, product)
    assert (stored.latest_price_minor, stored.pending_price_minor, stored.pending_since, stored.last_error) == (
        59900,
        None,
        None,
        None,
    )
    assert (count(ctx, PriceObservation), count(ctx, AlertEvent)) == (2, 1)
    assert len(ctx.notifiers["mock"].sent) == 1 and "Current price: $599.00" in ctx.notifiers["mock"].sent[0]


def test_single_odd_reading_is_discarded_without_an_alert(confirming):
    ctx, mock = confirming, confirming.providers["mock"]
    product = add(ctx, "pct_below_baseline", "25")
    add_low = alerts.NEW_LOW
    with ctx.sessions() as session:
        alerts.create_rule(session, session.get(Product, product.id), add_low, None)
        session.commit()
    mock.set_price(GPU, "8.00")  # a glitch: 99% lower
    assert check_product(ctx, product.id).status == "pending"
    mock.set_price(GPU, "800.00")
    ctx.now.advance(minutes=15)
    assert check_product(ctx, product.id).status == "ok"
    stored = reload(ctx, product)
    assert (stored.latest_price_minor, stored.lowest_minor, stored.pending_price_minor) == (80000, 80000, None)
    assert count(ctx, AlertEvent) == 0 and ctx.notifiers["mock"].sent == []
    with ctx.sessions() as session:
        assert [o.price_minor for o in session.scalars(select(PriceObservation))] == [80000, 80000]


def test_small_drops_and_first_prices_are_never_held(confirming):
    ctx, mock = confirming, confirming.providers["mock"]
    mock.set_price(GPU, "100.00")
    product = add(ctx)  # the first observation has nothing to compare with
    assert product.latest_price_minor == 10000 and product.pending_price_minor is None
    mock.set_price(GPU, "86.00")  # 14% lower: below the 15% threshold
    assert check_product(ctx, product.id).status == "ok"
    mock.set_price(GPU, "73.10")  # exactly 15% lower than 86.00
    assert check_product(ctx, product.id).status == "pending"
    mock.set_price(GPU, "120.00")  # increases are never held
    assert check_product(ctx, product.id).status == "ok"
    assert reload(ctx, product).latest_price_minor == 12000


def test_confirmation_is_off_by_default(ctx, mock):
    assert ctx.settings.confirm_drop_percent == 0
    product = add(ctx)
    mock.set_price(GPU, "100.00")
    assert check_product(ctx, product.id).status == "ok"  # default settings: stored at once
    assert reload(ctx, product).latest_price_minor == 10000


def test_pending_price_is_visible_in_the_api_and_on_the_product_page(confirming):
    held = add(confirming)
    confirming.providers["mock"].set_price(GPU, "100.00")
    check_product(confirming, held.id)
    confirming.settings.api_token = TOKEN
    with TestClient(create_app(ctx=confirming)) as client:
        data = client.get(f"/api/v1/products/{held.id}", headers={"Authorization": f"Bearer {TOKEN}"}).json()["data"]
    assert (data["latest_price"], data["pending_price"]) == ("800.00", "100.00")


def test_second_reading_prefers_a_different_provider(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "rest.canopyapi.co":
            calls.append("canopy")
            return httpx.Response(200, json=CANOPY_OK)  # $579.99
        if request.url.path.endswith("/runs"):
            calls.append("apify")
            return httpx.Response(201, json={"data": {"id": "r", "status": "SUCCEEDED", "defaultDatasetId": "d"}})
        return httpx.Response(200, json=[{"title": "x", "price": {"value": 575, "currency": "USD"}, "inStock": True}])

    ctx = paid_ctx(tmp_path, handler, confirm_drop_percent=Decimal("15"), max_monthly_api_spend_usd=Decimal("50"))
    assert provider_chain(ctx, None) == ["canopy", "apify"]
    assert provider_chain(ctx, None, "any", "canopy") == ["apify", "canopy"]
    assert provider_chain(ctx, None, "any", "apify") == ["canopy", "apify"]
    pid = tracked(ctx, latest_price_minor=80000, currency="USD", baseline_minor=80000)
    assert check_product(ctx, pid).status == "pending"
    confirmed = check_product(ctx, pid)
    assert (confirmed.status, confirmed.provider) == ("ok", "apify") and calls == ["canopy", "apify"]
    with ctx.sessions() as session:
        assert session.get(Product, pid).latest_price_minor == 57500
    ctx.close()


# --- Pacing ------------------------------------------------------------------------------------


def test_pacing_maths():
    start = datetime(2026, 11, 1, tzinfo=UTC)
    assert budget.month_fraction(start) == 0
    assert budget.month_fraction(datetime(2026, 11, 16, tzinfo=UTC)) == Decimal("0.5")
    assert budget.paced_allowance(Decimal(100), start) == 10  # 10% may be used ahead of schedule
    assert budget.paced_allowance(Decimal(100), datetime(2026, 11, 16, tzinfo=UTC)) == 60
    assert budget.paced_allowance(Decimal(100), datetime(2026, 11, 30, 23, tzinfo=UTC)) == 100  # never above the limit
    assert budget.paced_allowance(Decimal(5), start) == 1  # small limits can always start


def test_paced_provider_hands_over_and_is_used_again_later(tmp_path):
    calls: list[str] = []
    ctx = paid_ctx(
        tmp_path, both_ok(calls), pace_monthly_limits=True, canopy_max_monthly_requests=20,
        max_monthly_api_spend_usd=Decimal("50"), max_daily_api_requests=1000,
    )  # fmt: skip
    ctx.now.value = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    pid = tracked(ctx)
    assert [check_product(ctx, pid).provider for _ in range(4)] == ["canopy", "canopy", "apify", "apify"]
    with ctx.sessions() as session:
        rows = session.scalars(select(ProviderRequest.category)).all()
    assert "skipped_provider_paced" not in rows  # handing over is not a failure and leaves no noise
    ctx.now.advance(days=15)  # half the month: 10 accrued plus 2 burst
    assert [check_product(ctx, pid).provider for _ in range(11)] == ["canopy"] * 10 + ["apify"]
    ctx.close()


def test_all_providers_paced_skips_the_check_with_one_ledger_entry(tmp_path):
    calls: list[str] = []
    ctx = paid_ctx(
        tmp_path, both_ok(calls), pace_monthly_limits=True, canopy_max_monthly_requests=10, fallback_provider="",
        max_monthly_api_spend_usd=Decimal("50"),
    )  # fmt: skip
    ctx.now.value = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    pid = tracked(ctx)
    assert check_product(ctx, pid).status == "ok"
    skipped = check_product(ctx, pid)
    assert (skipped.status, skipped.category) == ("skipped", "provider_paced") and calls == ["canopy"]
    with ctx.sessions() as session:
        assert session.scalars(select(ProviderRequest.category)).all() == ["ok", "skipped_provider_paced"]
        assert session.get(Product, pid).consecutive_failures == 0
    ctx.close()


# --- Back in stock -----------------------------------------------------------------------------


def test_back_in_stock_alert(ctx, mock):
    product = add(ctx, "back_in_stock", None, cooldown_minutes=0)
    assert alerts.describe(alerts.AlertRule(kind="back_in_stock", threshold=0), "USD") == "Back in stock"
    ctx.now.advance(hours=6)
    check_product(ctx, product.id)  # in stock -> in stock
    assert ctx.notifiers["mock"].sent == []
    mock.set_mode("out_of_stock", GPU)
    ctx.now.advance(hours=6)
    assert check_product(ctx, product.id).category == "out_of_stock"
    assert reload(ctx, product).availability == "out_of_stock" and ctx.notifiers["mock"].sent == []
    mock.set_mode("ok", GPU)
    ctx.now.advance(hours=6)
    assert check_product(ctx, product.id).status == "ok"
    [message] = ctx.notifiers["mock"].sent
    assert (
        message.startswith("BACK IN STOCK") and "Availability: In stock" in message and "Rule: Back in stock" in message
    )
    ctx.now.advance(hours=6)
    check_product(ctx, product.id)
    assert len(ctx.notifiers["mock"].sent) == 1  # still in stock: no repeat


def test_back_in_stock_needs_a_confirmed_out_of_stock_state(ctx, mock):
    product = add(ctx, "back_in_stock", None, cooldown_minutes=0)
    for mode in ("outage", "no_price", "invalid"):  # failures are not "out of stock"
        mock.set_mode(mode, GPU)
        check_product(ctx, product.id)
    mock.set_mode("ok", GPU)
    check_product(ctx, product.id)
    assert ctx.notifiers["mock"].sent == []
    ctx.settings.api_token = TOKEN
    with TestClient(create_app(ctx=ctx)) as client:
        created = client.post(
            "/api/v1/alerts",
            json={"product_id": product.id, "kind": "back_in_stock"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert created.status_code == 201 and created.json()["data"]["description"] == "Back in stock"


# --- Scheduled backups -------------------------------------------------------------------------


def test_scheduled_backup_runs_on_its_interval_and_keeps_the_newest(ctx):
    settings = ctx.settings
    settings.backup_interval_hours, settings.backup_keep = 24, 3
    first = backup.run_scheduled(settings, ctx.now())
    assert first is not None and first.exists() and first.stat().st_mode & 0o777 == 0o600
    assert backup.run_scheduled(settings, ctx.now() + timedelta(hours=23)) is None
    unrelated = backup.backup_dir(settings) / "my-own-notes.db"
    unrelated.write_text("keep me")
    odd = backup.backup_dir(settings) / "monitor-manual-copy.db"
    odd.write_text("keep me too")
    for day in range(1, 6):
        assert backup.run_scheduled(settings, ctx.now() + timedelta(days=day)) is not None
    names = [path.name for _stamp, path in backup.list_backups(settings)]
    assert len(names) == 3 and names == sorted(names) and first.name not in names
    assert unrelated.exists() and odd.exists()  # files the application did not name are never deleted


def test_backups_can_be_turned_off_and_run_from_the_scheduler(ctx):
    ctx.settings.backup_interval_hours = 0
    assert backup.run_scheduled(ctx.settings, ctx.now()) is None and backup.list_backups(ctx.settings) == []
    ctx.settings.backup_interval_hours = 24
    scheduler = Scheduler(ctx)
    scheduler.run_once()
    scheduler.run_once()
    assert len(backup.list_backups(ctx.settings)) == 1
    scheduler.stop()


def test_a_failing_backup_does_not_stop_price_checks(ctx, mock, monkeypatch):
    product = add(ctx)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("app.scheduler.run_scheduled", boom)
    ctx.now.advance(hours=7)
    mock.set_price(GPU, "790.00")
    scheduler = Scheduler(ctx)
    assert [r.status for r in scheduler.run_once()] == ["ok"]
    assert reload(ctx, product).latest_price_minor == 79000
    scheduler.stop()
