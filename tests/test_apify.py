"""Apify adapter, provider fallback and cost controls, all against mocked HTTP."""

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app import budget
from app.checker import check_product, provider_chain
from app.config import Settings
from app.models import NotificationDelivery, PriceObservation, Product, ProviderRequest
from app.providers.apify import ApifyConfigError, ApifyProvider, dig, render_input, resolve_currency
from app.providers.base import ProviderError
from tests.conftest import make_ctx
from tests.test_providers import CANOPY_OK

ITEM = {
    "title": "Example GPU",
    "asin": "B0EXAMPLE1",
    "brand": "ASRock",
    "inStock": True,
    "price": {"value": 589.5, "currency": "$"},
    "listPrice": {"value": 800, "currency": "$"},
    "shippingPrice": None,
    "seller": {"name": "Amazon.com", "id": "ATVPDKIKX0DER"},
    "thumbnailImage": "https://m.media-amazon.com/images/I/example.jpg",
}
USD_ITEM = {**ITEM, "price": {"value": 589.5, "currency": "USD"}}


def run(status: str, usage: float | None = None, **extra) -> dict:
    return {"data": {"id": "run1", "status": status, "defaultDatasetId": "ds1", "usageTotalUsd": usage, **extra}}


class ApifyServer:
    """Scripted Apify API: one response list per endpoint kind."""

    def __init__(self, start, polls=(), items=None):
        self.start, self.polls, self.items = start, list(polls), items
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "rest.canopyapi.co":
            return httpx.Response(503)
        if path.endswith("/runs"):
            return self.start if isinstance(self.start, httpx.Response) else httpx.Response(201, json=self.start)
        if "/actor-runs/" in path:
            step = self.polls.pop(0)
            return step if isinstance(step, httpx.Response) else httpx.Response(200, json=step)
        if "/datasets/" in path:
            return self.items if isinstance(self.items, httpx.Response) else httpx.Response(200, json=self.items)
        return httpx.Response(404)


def apify(server, **overrides) -> ApifyProvider:
    options = {
        "base_url": "https://api.apify.com", "token": "apify-token-123", "actor_id": "junglee/amazon-crawler",
        "input_template": '{"categoryOrProductUrls": [{"url": "{url}"}], "maxItemsPerStartUrl": 1}', "field_map": "",
        "cost_per_request_usd": Decimal("0.01"), "max_concurrency": 1, "run_timeout_seconds": 120,
        "sleep": lambda seconds: None,
    }  # fmt: skip
    return ApifyProvider(httpx.Client(transport=httpx.MockTransport(server)), **{**options, **overrides})


def test_full_run_lifecycle_follows_the_documented_api():
    server = ApifyServer(run("READY"), [run("RUNNING"), run("SUCCEEDED", 0.0042)], [USD_ITEM])
    data = apify(server, max_charge_per_run_usd=Decimal("0.05")).fetch_product("B0EXAMPLE1", "US")
    start, poll_one, poll_two, items = server.requests
    assert (start.method, start.url.path) == ("POST", "/v2/actors/junglee~amazon-crawler/runs")
    assert dict(start.url.params) == {"waitForFinish": "60", "timeout": "120", "maxTotalChargeUsd": "0.05"}
    assert start.headers["authorization"] == "Bearer apify-token-123" and "token" not in str(start.url)
    assert json.loads(start.content) == {
        "categoryOrProductUrls": [{"url": "https://www.amazon.com/dp/B0EXAMPLE1"}],
        "maxItemsPerStartUrl": 1,
    }
    assert (poll_one.method, poll_one.url.path, poll_two.url.params["waitForFinish"]) == (
        "GET",
        "/v2/actor-runs/run1",
        "60",
    )
    assert items.url.path == "/v2/datasets/ds1/items"
    assert (data.price, data.currency, data.list_price, data.shipping) == (589.5, "USD", 800, None)
    assert (data.title, data.brand, data.seller, data.availability, data.condition) == (
        "Example GPU", "ASRock", "Amazon.com", "in_stock", "unknown",
    )  # fmt: skip
    assert data.actual_cost_usd == Decimal("0.0042")


def test_run_that_finishes_within_the_start_request_needs_no_polling():
    server = ApifyServer(run("SUCCEEDED", 0.001), [], [USD_ITEM])
    assert apify(server).fetch_product("B0EXAMPLE1", "US").price == 589.5
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    ("server", "category", "cost"),
    [
        (ApifyServer(run("RUNNING"), [run("FAILED", 0.002, statusMessage="Actor crashed")]), "run_failed", "0.002"),
        (ApifyServer(run("RUNNING"), [run("TIMED-OUT", 0.003)]), "run_failed", "0.003"),
        (ApifyServer(run("ABORTED", 0.001)), "run_failed", "0.001"),
        (ApifyServer(run("SUCCEEDED", 0.004), [], []), "not_found", "0.004"),
        (
            ApifyServer(run("SUCCEEDED", 0.004), [], [{"error": "not_found", "errorDescription": "x"}]),
            "not_found",
            "0.004",
        ),
        (ApifyServer(run("SUCCEEDED", 0.004), [], {"unexpected": True}), "malformed", "0.004"),
        (ApifyServer(run("SUCCEEDED", 0.004), [], httpx.Response(500)), "http_error", "0.004"),
        (ApifyServer({"data": {"status": "READY"}}), "malformed", None),
        (ApifyServer(httpx.Response(401)), "auth", None),
        (ApifyServer(httpx.Response(402)), "quota", None),
        (ApifyServer(httpx.Response(404)), "invalid_request", None),
        (ApifyServer(httpx.Response(500)), "http_error", None),
        (ApifyServer(httpx.Response(201, text="not json")), "malformed", None),
    ],
)
def test_failed_runs_and_errors(server, category, cost):
    with pytest.raises(ProviderError) as info:
        apify(server).fetch_product("B0EXAMPLE1", "US")
    assert info.value.category == category
    assert info.value.retryable is False  # a retry could start a second paid run
    assert info.value.actual_cost_usd == (Decimal(cost) if cost else None)


def test_only_a_rate_limited_start_is_retryable():
    with pytest.raises(ProviderError) as info:
        apify(ApifyServer(httpx.Response(429, headers={"retry-after": "3"}))).fetch_product("B0EXAMPLE1", "US")
    assert (info.value.category, info.value.retryable, info.value.retry_after) == ("rate_limited", True, 3)


def test_run_that_never_finishes_times_out_locally():
    ticks = iter(range(0, 100000, 50))
    server = ApifyServer(run("RUNNING"), [run("RUNNING", 0.001)] * 10)
    with pytest.raises(ProviderError) as info:
        apify(server, clock=lambda: next(ticks)).fetch_product("B0EXAMPLE1", "US")
    assert info.value.category == "timeout" and info.value.actual_cost_usd == Decimal("0.001")
    assert len(server.requests) < 8


def test_transient_polling_errors_do_not_abandon_a_paid_run():
    server = ApifyServer(run("RUNNING"), [httpx.Response(502), run("SUCCEEDED", 0.002)], [USD_ITEM])
    assert apify(server).fetch_product("B0EXAMPLE1", "US").price == 589.5


def test_configuration_and_field_mapping():
    assert not apify(ApifyServer(run("READY")), actor_id="").configured
    with pytest.raises(ProviderError) as info:
        apify(ApifyServer(run("READY")), token="").fetch_product("B0EXAMPLE1", "US")
    assert info.value.category == "not_configured"
    with pytest.raises(ApifyConfigError):
        apify(ApifyServer(run("READY")), input_template="{not json")
    with pytest.raises(ApifyConfigError):
        apify(ApifyServer(run("READY")), field_map="[1, 2]")
    custom = {"offer": {"amount": "450.00", "cur": "USD", "cond": "Used - Good"}, "name": "Thing", "stock": False}
    mapping = json.dumps(
        {
            "price": "offer.amount",
            "currency": "offer.cur",
            "condition": "offer.cond",
            "title": "name",
            "in_stock": "stock",
        }
    )
    template = '{"asins": ["{asin}"], "site": "{domain}", "country": "{marketplace}"}'
    server = ApifyServer(run("SUCCEEDED"), [], [custom])
    data = apify(server, field_map=mapping, input_template=template).fetch_product("B0EXAMPLE1", "US")
    assert json.loads(server.requests[0].content) == {"asins": ["B0EXAMPLE1"], "site": "amazon.com", "country": "US"}
    assert (data.price, data.currency, data.condition, data.title, data.availability) == (
        "450.00",
        "USD",
        "used",
        "Thing",
        "out_of_stock",
    )
    assert dig({"a": [{"b": 5}]}, "a.0.b") == 5 and dig({"a": 1}, "a.b") is None and dig({}, "") is None
    assert render_input({"x": ["{asin}", 3]}, {"asin": "B0"}) == {"x": ["B0", 3]}


# --- Through the pipeline: fallback, reported cost, budget -------------------------------------


def paid_ctx(tmp_path, handler, **overrides):
    values = {
        "provider": "canopy", "fallback_provider": "apify", "canopy_api_key": "canopy-key-123",
        "apify_token": "apify-token-123", "apify_actor_id": "junglee/amazon-crawler",
        "canopy_cost_per_request_usd": Decimal("0.01"), "apify_cost_per_run_usd": Decimal("0.02"),
        "allow_paid_api_requests": True, "provider_max_attempts": 1, "notify_mock_enabled": True,
    }  # fmt: skip
    settings = Settings(
        _env_file=None, data_dir=tmp_path, scheduler_enabled=False, log_level="WARNING", **{**values, **overrides}
    )
    return make_ctx(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))


def tracked(ctx, **fields) -> int:
    with ctx.sessions() as session:
        product = Product(
            asin="B0EXAMPLE1", title="Amazon product B0EXAMPLE1", url="https://www.amazon.com/dp/B0EXAMPLE1", **fields
        )
        session.add(product)
        session.commit()
        return product.id


def ledger(ctx) -> list[ProviderRequest]:
    with ctx.sessions() as session:
        return list(session.scalars(select(ProviderRequest).order_by(ProviderRequest.id)))


def test_fallback_to_apify_when_canopy_fails_and_provider_is_recorded(tmp_path):
    server = ApifyServer(run("SUCCEEDED", 0.0042), [], [USD_ITEM])  # the Canopy host answers 503
    ctx = paid_ctx(tmp_path, server)
    assert provider_chain(ctx, None) == ["canopy", "apify"]
    pid = tracked(ctx)
    result = check_product(ctx, pid)
    assert (result.status, result.provider) == ("ok", "apify")
    with ctx.sessions() as session:
        observation = session.scalars(select(PriceObservation)).one()
        assert (observation.provider, observation.price_minor, observation.price_basis) == ("apify", 58950, "item_only")
    rows = ledger(ctx)
    assert [(r.provider, r.success, r.category) for r in rows] == [
        ("canopy", False, "http_error"),
        ("apify", True, "ok"),
    ]
    assert (rows[1].estimated_cost_micro, rows[1].actual_cost_micro) == (20000, 4200)
    with ctx.sessions() as session:
        current = budget.usage(session, ctx.settings, ctx.now())
    assert current.month_cost_micro == 10000 + 4200  # Canopy estimate plus Apify's reported cost
    assert current.month_reported_micro == 4200 and current.is_estimate


def test_no_fallback_for_product_level_results(tmp_path):
    def handler(request):
        assert request.url.host == "rest.canopyapi.co", "the fallback must not be called"
        return httpx.Response(200, json={"data": {"amazonProduct": None}})

    ctx = paid_ctx(tmp_path, handler)
    assert check_product(ctx, tracked(ctx)).category == "not_found"
    assert [r.provider for r in ledger(ctx)] == ["canopy"]


def test_no_fallback_to_a_paid_provider_when_the_budget_is_exhausted(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=CANOPY_OK)

    ctx = paid_ctx(tmp_path, handler, max_monthly_api_spend_usd=Decimal("0.02"))
    pid = tracked(ctx)
    assert check_product(ctx, pid).status == "ok"
    assert check_product(ctx, pid).status == "ok"
    for _ in range(3):
        result = check_product(ctx, pid)
        assert (result.status, result.category) == ("skipped", "monthly_budget")
    assert calls == ["rest.canopyapi.co"] * 2  # Apify was never contacted
    assert [r.category for r in ledger(ctx)][2:] == ["skipped_monthly_budget"] * 3
    with ctx.sessions() as session:
        notices = session.scalars(select(NotificationDelivery).where(NotificationDelivery.event_id.is_(None))).all()
        assert len(notices) == 1 and notices[0].status == "sent"  # one budget notice, not one per skipped check
        assert "API BUDGET EXHAUSTED" in notices[0].body and "$0.0200" in notices[0].body
    ctx.now.advance(days=20)  # a new month sends a new notice only when the budget runs out again
    assert check_product(ctx, pid).status == "ok"


def test_per_product_provider_override_and_unconfigured_fallback(tmp_path):
    server = ApifyServer(run("SUCCEEDED", 0.001), [], [USD_ITEM])
    ctx = paid_ctx(tmp_path, server, fallback_provider="")
    pid = tracked(ctx, provider="apify")
    assert provider_chain(ctx, "apify") == ["apify"]
    assert check_product(ctx, pid).provider == "apify"
    assert all(request.url.host == "api.apify.com" for request in server.requests)


def test_currency_symbol_is_accepted_only_for_the_marketplace_currency(tmp_path):
    assert resolve_currency("$", "US") == "USD" and resolve_currency("USD", "US") == "USD"
    assert (
        resolve_currency("€", "US") is None
        and resolve_currency("$", "DE") is None
        and resolve_currency(None, "US") is None
    )
    server = ApifyServer(run("SUCCEEDED", 0.001), [], [ITEM])  # the Actor reports "$" on amazon.com
    ctx = paid_ctx(tmp_path, server, provider="apify", fallback_provider="")
    pid = tracked(ctx)
    assert check_product(ctx, pid).status == "ok"
    with ctx.sessions() as session:
        assert session.get(Product, pid).currency == "USD"
    server.items = [{**ITEM, "price": {"value": 500, "currency": "€"}}]
    result = check_product(ctx, pid)
    assert (result.status, result.category) == ("no_price", "no_currency")  # never guessed


def test_fallback_configuration_is_validated():
    for bad in ({"provider": "mock", "fallback_provider": "apify"}, {"provider": "canopy", "fallback_provider": "mock"},
                {"provider": "canopy", "fallback_provider": "canopy"}, {"provider": "bogus"}):  # fmt: skip
        with pytest.raises(ValueError):
            Settings(_env_file=None, **bad)
