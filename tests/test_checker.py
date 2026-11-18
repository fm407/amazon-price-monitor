"""Integration tests: product creation, history, scheduling, failures, budget, fallback."""

from datetime import timedelta
from decimal import Decimal
from threading import Event, Thread

import httpx
import pytest
from sqlalchemy import func, select

from app import budget, products
from app.checker import check_product
from app.config import Settings
from app.models import PriceObservation, Product, ProviderRequest
from app.providers.canopy import CanopyProvider
from app.providers.mock import MockProvider
from app.scheduler import Scheduler, recover
from tests.conftest import GPU, make_ctx
from tests.test_providers import CANOPY_OK


def add(ctx, text=GPU, **options) -> Product:
    with ctx.sessions() as session:
        product, _ = products.add_product(ctx, session, text, options)
        return product


def reload(ctx, product) -> Product:
    with ctx.sessions() as session:
        return session.get(Product, product.id)


def observations(ctx, product) -> list[PriceObservation]:
    with ctx.sessions() as session:
        return list(session.scalars(select(PriceObservation).where(PriceObservation.product_id == product.id)))


def requests(ctx) -> list[ProviderRequest]:
    with ctx.sessions() as session:
        return list(session.scalars(select(ProviderRequest).order_by(ProviderRequest.id)))


def test_add_product_stores_metadata_first_observation_and_baseline(ctx):
    product = add(ctx, "https://www.amazon.com/ASRock/dp/B0DEMOGPU1/ref=sr_1?tag=x-20")
    assert (product.asin, product.marketplace, product.url) == (GPU, "US", "https://www.amazon.com/dp/B0DEMOGPU1")
    assert product.title.startswith("ASRock Intel Arc Pro B60")
    assert (product.currency, product.brand, product.availability) == ("USD", "ASRock", "in_stock")
    assert product.initial_price_minor == product.baseline_minor == product.latest_price_minor == 80000
    assert product.lowest_minor == product.highest_minor == 80000
    assert product.last_success_at == ctx.now() and product.next_check_at > ctx.now()
    [observation] = observations(ctx, product)
    assert (observation.price_minor, observation.provider, observation.price_basis) == (80000, "mock", "delivered")
    assert (observation.seller, observation.condition) == ("Amazon.com", "new")


def test_duplicate_product_is_rejected(ctx):
    first = add(ctx)
    with pytest.raises(products.DuplicateProduct) as info:
        add(ctx, "https://www.amazon.com/gp/product/B0DEMOGPU1")
    assert info.value.product_id == first.id


def test_unknown_product_is_not_saved(ctx, mock):
    mock.set_mode("not_found", "B0MISSING1")
    with pytest.raises(products.ProductNotFound):
        add(ctx, "B0MISSING1")
    with ctx.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 0


def test_failed_first_lookup_saves_product_without_baseline_then_recovers(ctx, mock):
    mock.set_mode("outage")
    product = add(ctx)
    assert product.baseline_minor is None and product.latest_price_minor is None
    assert product.consecutive_failures == 1 and "outage" in product.last_error
    assert observations(ctx, product) == []
    mock.set_mode("ok")
    assert check_product(ctx, product.id).status == "ok"
    assert reload(ctx, product).baseline_minor == 80000


def test_price_change_updates_history_and_extremes(ctx, mock):
    product = add(ctx)
    for price in ("700.00", "850.00", "850.00"):
        ctx.now.advance(hours=6)
        mock.set_price(GPU, price)
        assert check_product(ctx, product.id).status == "ok"
    product = reload(ctx, product)
    assert [o.price_minor for o in observations(ctx, product)] == [80000, 70000, 85000, 85000]
    assert (product.latest_price_minor, product.previous_price_minor) == (85000, 70000)
    assert (product.lowest_minor, product.highest_minor, product.baseline_minor) == (70000, 85000, 80000)
    assert product.last_price_change_at == ctx.now() - timedelta(hours=6)


@pytest.mark.parametrize(
    ("setup", "category"),
    [
        (lambda m: m.set_mode("no_price", GPU), "no_price"),
        (lambda m: m.set_mode("out_of_stock", GPU), "out_of_stock"),
        (lambda m: m.set_price(GPU, 0), "zero_price"),
        (lambda m: m.set_price(GPU, "-5"), "invalid_price"),
        (lambda m: m.set_price(GPU, float("nan")), "invalid_price"),
        (lambda m: m.set_price(GPU, "abc"), "invalid_price"),
        (lambda m: m.set_price(GPU, "599.00", currency="EUR"), "currency_mismatch"),
        (lambda m: m.set_price(GPU, "599.00", currency=None), "no_currency"),
        (lambda m: m.set_price(GPU, "399.00", condition="used"), "condition_excluded"),
    ],
)
def test_invalid_responses_never_overwrite_the_last_valid_price(ctx, mock, setup, category):
    product = add(ctx)
    setup(mock)
    result = check_product(ctx, product.id)
    assert (result.status, result.category) == ("no_price", category)
    product = reload(ctx, product)
    assert product.latest_price_minor == 80000 and product.lowest_minor == 80000
    assert len(observations(ctx, product)) == 1
    assert product.last_error and product.consecutive_failures == 0


def test_seller_filter_and_used_offers(ctx, mock):
    product = add(ctx, seller_filter="amazon")
    mock.set_price(GPU, "500.00", seller="Bargain Store")
    assert check_product(ctx, product.id).category == "seller_excluded"
    mock.set_price(GPU, "500.00", seller=None)
    assert check_product(ctx, product.id).category == "seller_excluded"
    mock.set_price(GPU, "500.00", seller="Amazon Resale")
    assert check_product(ctx, product.id).category == "seller_excluded"
    mock.set_price(GPU, "650.00", seller="Amazon.com")
    assert check_product(ctx, product.id).status == "ok"
    used = add(ctx, "B0DEMOSSD1", include_used=True)
    mock.set_price("B0DEMOSSD1", "150.00", condition="used")
    assert check_product(ctx, used.id).status == "ok"


def test_shipping_is_added_when_known_and_marked_incomplete_when_unknown(ctx, mock):
    mock.set_price(GPU, "590.00", shipping="12.50")
    product = add(ctx)
    [with_shipping] = observations(ctx, product)
    assert (with_shipping.item_price_minor, with_shipping.shipping_minor) == (59000, 1250)
    assert (with_shipping.effective_minor, with_shipping.price_minor, with_shipping.price_basis) == (
        60250,
        60250,
        "delivered",
    )
    mock.set_price(GPU, "590.00", shipping=None)
    check_product(ctx, product.id)
    unknown = observations(ctx, product)[-1]
    assert (unknown.shipping_minor, unknown.effective_minor, unknown.price_basis) == (None, None, "item_only")
    assert unknown.price_minor == 59000


def test_transient_failure_is_retried_with_backoff_then_scheduled_sooner(ctx, mock):
    product = add(ctx)
    waits = []
    ctx.sleep = waits.append
    mock.set_mode("outage")
    before = mock.calls
    result = check_product(ctx, product.id)
    assert (result.status, result.category) == ("error", "network")
    assert mock.calls - before == 3 and len(waits) == 2 and waits[0] < waits[1]
    product = reload(ctx, product)
    assert product.consecutive_failures == 1 and product.latest_price_minor == 80000
    assert product.next_check_at - ctx.now() < timedelta(minutes=20)
    assert [r.retry_count for r in requests(ctx)[-3:]] == [0, 1, 2]


def test_rate_limit_honours_retry_after_then_pauses_provider(ctx, mock):
    product = add(ctx)
    waits = []
    ctx.sleep = waits.append
    mock.set_mode("rate_limit")
    before = mock.calls
    assert check_product(ctx, product.id).category == "rate_limited"
    assert waits == [1, 1]
    calls_after_first = mock.calls - before
    result = check_product(ctx, product.id)
    assert (result.status, result.category) == ("skipped", "provider_blocked")
    assert mock.calls - before == calls_after_first  # no request while the provider is paused
    ctx.now.advance(minutes=2)
    mock.set_mode("ok")
    assert check_product(ctx, product.id).status == "ok"


def test_authentication_failure_is_not_retried(ctx, mock):
    product = add(ctx)
    mock.set_mode("auth")
    before = mock.calls
    assert check_product(ctx, product.id).category == "auth"
    assert check_product(ctx, product.id).category == "provider_blocked"
    assert mock.calls - before == 1


def test_repeated_not_found_pauses_the_product(ctx, mock):
    product = add(ctx)
    mock.set_mode("not_found", GPU)
    for _ in range(3):
        check_product(ctx, product.id)
    product = reload(ctx, product)
    assert product.status == "paused" and "Paused" in product.last_error


def test_concurrent_checks_of_one_product_do_not_overlap(ctx):
    entered, release = Event(), Event()

    class SlowProvider(MockProvider):
        def fetch_product(self, asin, marketplace):
            entered.set()
            release.wait(5)
            return super().fetch_product(asin, marketplace)

    product = add(ctx)
    ctx.providers["mock"] = SlowProvider()
    results = []
    worker = Thread(target=lambda: results.append(check_product(ctx, product.id)))
    worker.start()
    assert entered.wait(5)
    assert check_product(ctx, product.id).status == "busy"
    release.set()
    worker.join(5)
    assert results[0].status == "ok"
    assert len(observations(ctx, product)) == 2


def test_product_deleted_during_check_is_handled(ctx):
    product = add(ctx)

    class DeletingProvider(MockProvider):
        def fetch_product(self, asin, marketplace):
            with ctx.sessions() as session:
                session.delete(session.get(Product, product.id))
                session.commit()
            return super().fetch_product(asin, marketplace)

    ctx.providers["mock"] = DeletingProvider()
    assert check_product(ctx, product.id).status == "deleted"
    assert check_product(ctx, product.id).status == "deleted"
    assert requests(ctx)[-1].product_id is None  # request history survives the product


def test_provider_exception_does_not_crash_the_check(ctx):
    product = add(ctx)

    class BrokenProvider(MockProvider):
        def fetch_product(self, asin, marketplace):
            raise RuntimeError("boom")

    ctx.providers["mock"] = BrokenProvider()
    result = check_product(ctx, product.id)
    assert (result.status, result.category) == ("error", "internal")
    assert reload(ctx, product).latest_price_minor == 80000


def test_scheduler_checks_only_due_active_products(ctx, mock):
    due, later, paused = add(ctx), add(ctx, "B0DEMOSSD1"), add(ctx, "B0DEMORAM1")
    with ctx.sessions() as session:
        session.get(Product, later.id).next_check_at = ctx.now() + timedelta(days=2)
        products.set_status(ctx, session.get(Product, paused.id), "paused")
        session.commit()
    scheduler = Scheduler(ctx)
    assert scheduler.run_once() == []
    ctx.now.advance(hours=7)
    mock.set_price(GPU, "777.00")
    results = scheduler.run_once()
    assert [r.status for r in results] == ["ok"]
    assert reload(ctx, due).latest_price_minor == 77700
    assert reload(ctx, due).next_check_at > ctx.now() + timedelta(hours=5)
    assert scheduler.run_once() == []  # nothing is due again: no duplicate job
    scheduler.stop()


def test_per_product_interval_is_respected(ctx):
    product = add(ctx, check_interval_minutes=30)
    delta = product.next_check_at - ctx.now()
    assert timedelta(minutes=30) <= delta <= timedelta(minutes=33)


def test_restart_recovery_spreads_overdue_checks_and_closes_interrupted_requests(ctx):
    first, second = add(ctx), add(ctx, "B0DEMOSSD1")
    with ctx.sessions() as session:
        session.add(
            ProviderRequest(provider="mock", asin=GPU, started_at=ctx.now(), is_paid=True, estimated_cost_micro=5)
        )
        session.commit()
    ctx.now.advance(days=3)
    recover(ctx)
    times = [reload(ctx, p).next_check_at for p in (first, second)]
    assert all(ctx.now() < t <= ctx.now() + timedelta(minutes=10) for t in times) and times[0] != times[1]
    interrupted = requests(ctx)[-1]
    assert (interrupted.success, interrupted.category, interrupted.estimated_cost_micro) == (False, "interrupted", 5)


# --- Paid providers: budget guard and fallback -------------------------------------------------


def paid_settings(tmp_path, **overrides) -> Settings:
    values = {
        "provider": "canopy", "canopy_api_key": "test-key-123", "canopy_cost_per_request_usd": Decimal("1.00"),
        "allow_paid_api_requests": True, "max_monthly_api_spend_usd": Decimal("3.00"), "provider_max_attempts": 1,
    }  # fmt: skip
    return Settings(
        _env_file=None, data_dir=tmp_path, scheduler_enabled=False, log_level="WARNING", **{**values, **overrides}
    )


def canopy_ctx(tmp_path, handler, **overrides):
    settings = paid_settings(tmp_path, **overrides)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    ctx = make_ctx(settings, http=client)
    assert isinstance(ctx.providers["canopy"], CanopyProvider)
    return ctx


def ok_handler(calls):
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=CANOPY_OK)

    return handler


def test_paid_requests_are_disabled_by_default(tmp_path):
    assert Settings(_env_file=None).allow_paid_api_requests is False
    calls = []
    ctx = canopy_ctx(tmp_path, ok_handler(calls), allow_paid_api_requests=False)
    product = add(ctx, "B0EXAMPLE1")
    assert calls == [] and product.baseline_minor is None
    assert "disabled" in product.last_error and product.consecutive_failures == 0
    assert requests(ctx)[-1].category == "skipped_paid_disabled"


def test_monthly_budget_is_a_hard_limit_and_survives_restart(tmp_path):
    calls = []
    ctx = canopy_ctx(tmp_path, ok_handler(calls))
    product = add(ctx, "B0EXAMPLE1")
    assert product.latest_price_minor == 57999 and product.latest_seller == "Amazon.com"
    assert check_product(ctx, product.id).status == "ok"
    assert check_product(ctx, product.id).status == "ok"
    result = check_product(ctx, product.id)
    assert (result.status, result.category) == ("skipped", "monthly_budget")
    assert len(calls) == 3
    ctx.close()

    restarted = canopy_ctx(tmp_path, ok_handler(calls))  # same database, new process state
    assert check_product(restarted, product.id).category == "monthly_budget"
    assert len(calls) == 3
    with restarted.sessions() as session:
        current = budget.usage(session, restarted.settings, restarted.now())
    assert (current.month_paid_requests, current.month_cost_micro, current.remaining_micro) == (3, 3_000_000, 0)
    assert current.is_estimate
    restarted.now.advance(days=20)  # next calendar month: the budget resets
    assert check_product(restarted, product.id).status == "ok"


def test_daily_request_limit(tmp_path):
    calls = []
    ctx = canopy_ctx(tmp_path, ok_handler(calls), max_daily_api_requests=2, max_monthly_api_spend_usd=Decimal("50"))
    product = add(ctx, "B0EXAMPLE1")
    check_product(ctx, product.id)
    assert check_product(ctx, product.id).category == "daily_limit"
    ctx.now.advance(days=1)
    assert check_product(ctx, product.id).status == "ok"
    assert len(calls) == 3


def test_concurrent_checks_cannot_exceed_the_budget(tmp_path):
    calls = []
    ctx = canopy_ctx(tmp_path, ok_handler(calls), max_monthly_api_spend_usd=Decimal("5.00"), canopy_max_concurrency=8)
    ids = []
    with ctx.sessions() as session:
        for index in range(12):
            product = Product(asin=f"B0CONCUR{index:02d}", title="x", url="https://www.amazon.com/dp/x")
            session.add(product)
            session.flush()
            ids.append(product.id)
        session.commit()
    threads = [Thread(target=check_product, args=(ctx, product_id)) for product_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(calls) == 5
    with ctx.sessions() as session:
        assert budget.usage(session, ctx.settings, ctx.now()).month_cost_micro == 5_000_000


def test_failed_paid_requests_still_count_against_the_budget(tmp_path):
    ctx = canopy_ctx(tmp_path, lambda request: httpx.Response(500), provider_max_attempts=2)
    product = add(ctx, "B0EXAMPLE1")
    assert product.consecutive_failures == 1
    with ctx.sessions() as session:
        current = budget.usage(session, ctx.settings, ctx.now())
    assert (current.month_paid_requests, current.month_failed, current.month_cost_micro) == (2, 2, 2_000_000)
    assert [(r.http_status, r.category, r.success) for r in requests(ctx)] == [(500, "http_error", False)] * 2
