"""SerpApi, Easyparser and Scrapingdog adapters against responses shaped like their documentation."""

from decimal import Decimal

import httpx
import pytest

from app.checker import check_product, provider_chain
from app.config import Settings
from app.models import PriceObservation, Product
from app.providers.base import ProviderError, availability_from_text
from app.providers.easyparser import EasyparserProvider
from app.providers.scrapingdog import ScrapingdogProvider, leading_amount
from app.providers.serpapi import SerpApiProvider
from tests.conftest import make_ctx
from tests.test_apify import tracked

KEY = "free-tier-key-123456"
SERPAPI_OK = {
    "search_metadata": {"status": "Success"},
    "product_results": {
        "title": "Amazon Fresh, Colombia Ground Coffee, Medium Roast, 32 Oz",
        "thumbnail": "https://m.media-amazon.com/images/I/51QbVAHxoVL.jpg",
        "price": "$15.27",
        "extracted_price": 15.27,
        "old_price": "$16.53",
        "extracted_old_price": 16.53,
        "stock": "In Stock",
        "delivery": ["FREE delivery Monday, October 26 on orders shipped by Amazon over $35"],
    },
    "purchase_options": {
        "buy_new": {
            "price": "$15.27",
            "extracted_price": 15.27,
            "stock": "In Stock",
            "features": {"shipper_seller": {"text": "Amazon.com"}},
        }
    },
}
EASYPARSER_OK = {
    "request_info": {"success": True, "status_code": 200, "error_details": [], "credit_used_this_request": 1},
    "result": {
        "detail": {
            "title": "Example Headphones",
            "asin": "B0EXAMPLE1",
            "brand": "ExampleBrand",
            "images": [
                {"link": "https://m.media-amazon.com/images/I/side.jpg", "variant": "PT01"},
                {"link": "https://m.media-amazon.com/images/I/31J5v542jbL._AC_.jpg", "variant": "MAIN"},
            ],
            "buybox_winner": {
                "condition": {"is_new": True},
                "price": {"symbol": "$", "value": 45, "currency": "USD", "raw": "$45.00"},
                "rrp": {"symbol": "$", "value": "59.99", "currency": "USD", "raw": "59.99"},
                "shipping": {"raw": "FREE"},
                "availability": {"raw": "In Stock"},
            },
        }
    },
}
SCRAPINGDOG_OK = {
    "title": "Apple 2025 MacBook Pro Laptop",
    "brand": "Apple Store",
    "main_image": "https://m.media-amazon.com/images/I/6112T6g2P-L.jpg",
    "price": "$1,849.99 with 7 percent savings",
    "price_symbol": "$",
    "previous_price": "$1,999.00",
    "availability_status": "In Stock",
    "sold_by": "Amazon.com",
    "shipping_info": "FREE Thursday, September 3",
    "purchase_options": {"single_offer": {"extracted_price": 1849.99}},
}
CASES = [(SerpApiProvider, SERPAPI_OK), (EasyparserProvider, EASYPARSER_OK), (ScrapingdogProvider, SCRAPINGDOG_OK)]


def make(provider_class, response, seen=None, key=KEY):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return response(request) if callable(response) else response

    return provider_class(httpx.Client(transport=httpx.MockTransport(handler)), key, Decimal("0"))


def ok(body) -> httpx.Response:
    return httpx.Response(200, json=body)


def test_serpapi_request_and_mapping():
    seen: list[httpx.Request] = []
    data = make(SerpApiProvider, ok(SERPAPI_OK), seen).fetch_product("B0EXAMPLE1", "US")
    assert seen[0].url.path == "/search" and seen[0].url.host == "serpapi.com"
    assert dict(seen[0].url.params) == {
        "engine": "amazon_product",
        "asin": "B0EXAMPLE1",
        "amazon_domain": "amazon.com",
        "api_key": KEY,
    }
    assert (data.price, data.currency, data.list_price) == (15.27, "USD", 16.53)
    assert (data.seller, data.condition, data.availability) == ("Amazon.com", "new", "in_stock")
    assert data.shipping is None  # "FREE delivery ... over $35" is conditional: never assumed free
    assert data.price_basis == "buy_new_item_price"


def test_serpapi_without_a_new_offer_uses_the_listed_price_with_unknown_condition():
    body = {"product_results": {**SERPAPI_OK["product_results"], "stock": "Currently unavailable."}}
    data = make(SerpApiProvider, ok(body)).fetch_product("B0EXAMPLE1", "US")
    assert (data.price, data.condition, data.seller, data.availability) == (15.27, "unknown", None, "out_of_stock")
    euro = {"product_results": {**SERPAPI_OK["product_results"], "price": "€15,27"}}
    assert make(SerpApiProvider, ok(euro)).fetch_product("B0EXAMPLE1", "US").currency is None  # never guessed


def test_easyparser_request_and_mapping():
    seen: list[httpx.Request] = []
    data = make(EasyparserProvider, ok(EASYPARSER_OK), seen).fetch_product("B0EXAMPLE1", "US")
    assert str(seen[0].url).startswith("https://realtime.easyparser.com/v1/request?")
    assert dict(seen[0].url.params) == {
        "api_key": KEY,
        "platform": "AMZ",
        "operation": "DETAIL",
        "domain": ".com",
        "asin": "B0EXAMPLE1",
    }
    assert (data.price, data.currency, data.list_price, data.shipping) == (45, "USD", "59.99", "0")
    assert (data.title, data.brand, data.condition, data.availability, data.seller) == (
        "Example Headphones",
        "ExampleBrand",
        "new",
        "in_stock",
        None,
    )
    assert data.image_url == "https://m.media-amazon.com/images/I/31J5v542jbL._AC_.jpg"


def test_easyparser_failure_inside_a_200_response():
    failed = {
        "request_info": {
            "success": False,
            "status_code": 0,
            "error_details": [{"error_code": 5, "message": "Page could not be parsed"}],
        }
    }
    with pytest.raises(ProviderError) as info:
        make(EasyparserProvider, ok(failed)).fetch_product("B0EXAMPLE1", "US")
    assert info.value.category == "http_error" and "Page could not be parsed" in info.value.message
    missing = {"request_info": {"success": False, "status_code": 404, "error_details": []}}
    with pytest.raises(ProviderError) as info:
        make(EasyparserProvider, ok(missing)).fetch_product("B0EXAMPLE1", "US")
    assert info.value.category == "not_found"
    used = {
        **EASYPARSER_OK,
        "result": {
            "detail": {"title": "x", "buybox_winner": {"condition": {"is_new": False}, "shipping": {"raw": "$4.99"}}}
        },
    }
    data = make(EasyparserProvider, ok(used)).fetch_product("B0EXAMPLE1", "US")
    assert (data.condition, data.shipping, data.price) == ("used", None, None)


def test_scrapingdog_request_and_mapping():
    seen: list[httpx.Request] = []
    data = make(ScrapingdogProvider, ok(SCRAPINGDOG_OK), seen).fetch_product("B0EXAMPLE1", "US")
    assert str(seen[0].url).startswith("https://api.scrapingdog.com/amazon/product?")
    assert dict(seen[0].url.params) == {"api_key": KEY, "asin": "B0EXAMPLE1", "domain": "com", "country": "us"}
    assert (data.price, data.currency, data.list_price, data.shipping) == (1849.99, "USD", "1999.00", None)
    assert (data.seller, data.availability, data.condition, data.brand) == (
        "Amazon.com",
        "in_stock",
        "unknown",
        "Apple Store",
    )


def test_scrapingdog_display_price_fallback_is_strict():
    body = {**SCRAPINGDOG_OK, "purchase_options": {}}
    data = make(ScrapingdogProvider, ok(body)).fetch_product("B0EXAMPLE1", "US")
    assert (data.price, data.price_basis) == ("1849.99", "listed_item_price")
    assert leading_amount("$1,849.99 with 7 percent savings", "$") == "1849.99"
    assert leading_amount("Save 7% on $1,849.99", "$") is None  # the number is not the leading price
    assert (
        leading_amount("1,849.99", "$") is None
        and leading_amount("$abc", "$") is None
        and leading_amount(None, "$") is None
    )
    assert leading_amount("$20", None) is None
    with pytest.raises(ProviderError) as info:
        make(ScrapingdogProvider, ok({"message": "nothing here"})).fetch_product("B0EXAMPLE1", "US")
    assert info.value.category == "not_found"


@pytest.mark.parametrize(("provider_class", "_body"), CASES)
@pytest.mark.parametrize(
    ("response", "category", "retryable"),
    [
        (httpx.Response(401, json={"error": "Invalid API key"}), "auth", False),
        (httpx.Response(403), "auth", False),
        (httpx.Response(402), "quota", False),
        (httpx.Response(404), "not_found", False),
        (httpx.Response(400), "invalid_request", False),
        (httpx.Response(410), "http_error", False),
        (httpx.Response(429, headers={"retry-after": "9"}), "rate_limited", True),
        (httpx.Response(503), "http_error", True),
        (httpx.Response(200, text="<html>"), "malformed", False),
        (httpx.Response(200, json=[1, 2]), "malformed", False),
        (httpx.Response(200, json={}), None, False),
    ],
)
def test_shared_error_handling(provider_class, _body, response, category, retryable):
    with pytest.raises(ProviderError) as info:
        make(provider_class, response).fetch_product("B0EXAMPLE1", "US")
    if category is None:
        assert info.value.category in ("not_found", "http_error")  # an empty document holds no product
        return
    assert (info.value.category, info.value.retryable) == (category, retryable)
    if category in ("auth", "quota"):
        assert info.value.block_seconds > 0
    assert KEY not in info.value.message


@pytest.mark.parametrize(("provider_class", "_body"), CASES)
def test_network_errors_missing_key_and_unknown_marketplace(provider_class, _body):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError(f"refused {request.url}", request=request)

    for handler, category in ((timeout, "timeout"), (refused, "network")):
        with pytest.raises(ProviderError) as info:
            make(provider_class, handler).fetch_product("B0EXAMPLE1", "US")
        assert (info.value.category, info.value.retryable) == (category, True)
        assert KEY not in info.value.message  # the key is in the URL, and the URL never reaches the message
    unconfigured = make(provider_class, ok({}), key="")
    assert unconfigured.configured is False
    with pytest.raises(ProviderError) as info:
        unconfigured.fetch_product("B0EXAMPLE1", "US")
    assert info.value.category == "not_configured"
    with pytest.raises(ProviderError) as info:
        make(provider_class, ok({})).fetch_product("B0EXAMPLE1", "ZZ")
    assert info.value.category == "unsupported_marketplace"


def test_availability_phrases():
    assert availability_from_text("In Stock") == "in_stock"
    assert availability_from_text("Only 3 left in stock - order soon.") == "in_stock"
    assert availability_from_text("Currently unavailable.") == "out_of_stock"
    assert availability_from_text("Temporarily out of stock.") == "out_of_stock"
    assert availability_from_text("Usually ships within 2 weeks") == "unknown"
    assert availability_from_text(None) == "unknown"


def test_three_free_plans_are_used_one_after_another(tmp_path):
    """SerpApi first, then Easyparser, then Scrapingdog. Each stops at its own monthly allowance."""
    bodies = {
        "serpapi.com": SERPAPI_OK,
        "realtime.easyparser.com": EASYPARSER_OK,
        "api.scrapingdog.com": SCRAPINGDOG_OK,
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, json=bodies[request.url.host])

    settings = Settings(
        _env_file=None, data_dir=tmp_path, scheduler_enabled=False, log_level="WARNING",
        provider="serpapi", fallback_provider="easyparser, scrapingdog", allow_paid_api_requests=True,
        serpapi_api_key=KEY, easyparser_api_key=KEY, scrapingdog_api_key=KEY, provider_max_attempts=1,
        serpapi_cost_per_request_usd=Decimal("0"), easyparser_cost_per_request_usd=Decimal("0"),
        scrapingdog_cost_per_request_usd=Decimal("0"),
        serpapi_max_monthly_requests=2, easyparser_max_monthly_requests=1, scrapingdog_max_monthly_requests=1,
    )  # fmt: skip
    ctx = make_ctx(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider_chain(ctx, None) == ["serpapi", "easyparser", "scrapingdog"]
    assert KEY in settings.secret_values()
    first, second = tracked(ctx), None
    with ctx.sessions() as session:
        other = Product(
            asin="B0EXAMPLE2", title="Amazon product B0EXAMPLE2", url="https://www.amazon.com/dp/B0EXAMPLE2"
        )
        session.add(other)
        session.commit()
        second = other.id
    assert [check_product(ctx, first).provider for _ in range(2)] == ["serpapi", "serpapi"]
    assert check_product(ctx, second).provider == "easyparser"  # SerpApi's allowance is used up
    # The first product is tracked in USD from SerpApi; Scrapingdog's "$" resolves to USD as well.
    assert check_product(ctx, first).provider == "scrapingdog"
    skipped = check_product(ctx, first)
    assert (skipped.status, skipped.category) == ("skipped", "provider_quota")
    assert calls == ["serpapi.com", "serpapi.com", "realtime.easyparser.com", "api.scrapingdog.com"]
    with ctx.sessions() as session:
        assert [o.provider for o in session.query(PriceObservation).order_by(PriceObservation.id)] == [
            "serpapi", "serpapi", "easyparser", "scrapingdog",
        ]  # fmt: skip
        assert (
            session.get(Product, second).latest_price_minor == 4500
            and session.get(Product, first).latest_price_minor == 184999
        )
    ctx.close()
