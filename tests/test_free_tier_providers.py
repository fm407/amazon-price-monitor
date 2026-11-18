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
        pace_monthly_limits=False,
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


# --- Regression tests for the code review findings ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$1,849.99 with 7 percent savings", "1849.99"),
        ("$20", "20"),
        ("$1849.5", "1849.5"),
        ("$1,234,567.00", "1234567.00"),
        ("€12,99", None),  # decimal comma: would be read 100 times too high
        ("€1.299,00", None),  # dot as thousands separator: would be read 1000 times too low
        ("$ 1.299,00", None),
        ("$10.00 - $20.00", None),  # a range is not one price
        ("$10.00 to $20.00", None),
        ("$10.00–$20.00", None),
        ("$12,99", None),
        ("$1,84", None),
        ("$12.999", None),
        ("$12abc", None),
    ],
)
def test_leading_amount_accepts_only_unambiguous_us_amounts(text, expected):
    assert leading_amount(text, text[0]) == expected


def chain_ctx(tmp_path, handler, **overrides):
    values = {
        "provider": "easyparser", "fallback_provider": "scrapingdog", "allow_paid_api_requests": True,
        "easyparser_api_key": "easyparser-key-AAAA1111", "scrapingdog_api_key": "scrapingdog-key-BBBB2222",
        "serpapi_api_key": "serpapi-key-CCCC3333", "provider_max_attempts": 1, "pace_monthly_limits": False,
    }  # fmt: skip
    settings = Settings(
        _env_file=None, data_dir=tmp_path, scheduler_enabled=False, log_level="WARNING", **{**values, **overrides}
    )
    return make_ctx(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_each_provider_receives_its_own_key(tmp_path):
    seen: dict[str, str] = {}
    bodies = {
        "serpapi.com": SERPAPI_OK,
        "realtime.easyparser.com": EASYPARSER_OK,
        "api.scrapingdog.com": SCRAPINGDOG_OK,
    }

    def handler(request):
        seen[request.url.host] = request.url.params["api_key"]
        return httpx.Response(200, json=bodies[request.url.host])

    ctx = chain_ctx(tmp_path, handler, provider="serpapi", fallback_provider="easyparser,scrapingdog",
                    serpapi_max_monthly_requests=1, easyparser_max_monthly_requests=1)  # fmt: skip
    first = tracked(ctx)
    with ctx.sessions() as session:
        others = [Product(asin=f"B0EXAMPLE{i}", title="t", url="https://www.amazon.com/dp/x") for i in (2, 3)]
        session.add_all(others)
        session.commit()
        ids = [first, *(p.id for p in others)]
    assert [check_product(ctx, pid).provider for pid in ids] == ["serpapi", "easyparser", "scrapingdog"]
    assert seen == {
        "serpapi.com": "serpapi-key-CCCC3333",
        "realtime.easyparser.com": "easyparser-key-AAAA1111",
        "api.scrapingdog.com": "scrapingdog-key-BBBB2222",
    }
    ctx.close()


def test_provider_error_text_that_echoes_the_key_is_redacted_before_storage(tmp_path):
    from app.logging_config import configure_logging
    from app.models import ProviderRequest

    echo = {
        "request_info": {
            "success": False,
            "status_code": 0,
            "error_details": [{"message": "Invalid api_key: easyparser-key-AAAA1111"}],
        }
    }
    ctx = chain_ctx(tmp_path, lambda request: httpx.Response(200, json=echo), fallback_provider="")
    configure_logging("WARNING", ctx.settings.secret_values())
    pid = tracked(ctx)
    assert check_product(ctx, pid).status == "error"
    with ctx.sessions() as session:
        stored = [session.get(Product, pid).last_error, session.query(ProviderRequest).one().detail]
    assert all("easyparser-key-AAAA1111" not in text and "***" in text for text in stored), stored
    configure_logging("WARNING", [])
    ctx.close()


def test_amazon_only_products_skip_providers_that_cannot_name_the_seller(tmp_path):
    calls: list[str] = []
    bodies = {"realtime.easyparser.com": EASYPARSER_OK, "api.scrapingdog.com": SCRAPINGDOG_OK}

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=bodies[request.url.host])

    ctx = chain_ctx(tmp_path, handler)
    assert provider_chain(ctx, None) == ["easyparser", "scrapingdog"]
    assert provider_chain(ctx, None, "amazon") == ["scrapingdog"]
    pid = tracked(ctx, seller_filter="amazon")
    result = check_product(ctx, pid)
    assert (result.status, result.provider) == ("ok", "scrapingdog")
    assert calls == ["api.scrapingdog.com"]  # no Easyparser credit was spent on a price that must be rejected
    ctx.close()

    (tmp_path / "b").mkdir()
    only_easyparser = chain_ctx(tmp_path / "b", handler, fallback_provider="")
    pid = tracked(only_easyparser, seller_filter="amazon")
    result = check_product(only_easyparser, pid)
    assert (result.status, result.category) == (
        "error",
        "not_configured",
    ) and "No configured price provider" in result.message
    assert len(calls) == 1
    only_easyparser.close()


def test_a_real_error_is_not_hidden_by_a_later_quota_skip(tmp_path):
    def handler(request):
        if request.url.host == "realtime.easyparser.com":
            raise httpx.ReadTimeout("slow", request=request)
        raise AssertionError("Scrapingdog must not be called: its allowance is used up")

    ctx = chain_ctx(tmp_path, handler, scrapingdog_max_monthly_requests=0)
    pid = tracked(ctx)
    result = check_product(ctx, pid)
    assert (result.status, result.category) == ("error", "timeout")  # the timeout, not the skip
    with ctx.sessions() as session:
        assert session.get(Product, pid).consecutive_failures == 1
    ctx.close()


def test_quota_skip_outranks_a_missing_key_and_does_not_count_as_a_failure(tmp_path):
    ctx = chain_ctx(
        tmp_path, lambda request: httpx.Response(500), easyparser_max_monthly_requests=0, scrapingdog_api_key=""
    )
    pid = tracked(ctx)
    for _ in range(3):
        result = check_product(ctx, pid)
        assert (result.status, result.category) == ("skipped", "provider_quota")
    with ctx.sessions() as session:
        product = session.get(Product, pid)
        assert product.consecutive_failures == 0
        assert (product.next_check_at - ctx.now()).total_seconds() >= 6 * 3600  # normal interval, no fast retry
    ctx.close()


def test_retry_denied_by_the_provider_limit_keeps_the_original_error(tmp_path):
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(503)

    ctx = chain_ctx(tmp_path, handler, fallback_provider="", provider_max_attempts=3, easyparser_max_monthly_requests=1)
    result = check_product(ctx, tracked(ctx))
    assert (result.status, result.category) == ("error", "http_error")
    assert calls == ["realtime.easyparser.com"]  # the second attempt was denied before any request
    ctx.close()


def test_in_flight_and_interrupted_requests_count_toward_a_provider_limit(tmp_path):
    from app.models import ProviderRequest

    calls: list[str] = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=SCRAPINGDOG_OK)

    ctx = chain_ctx(tmp_path, handler, provider="scrapingdog", fallback_provider="", scrapingdog_max_monthly_requests=2)
    with ctx.sessions() as session:
        session.add(ProviderRequest(provider="scrapingdog", asin="B0PENDING1", started_at=ctx.now(), is_paid=True))
        session.add(ProviderRequest(provider="scrapingdog", asin="B0INTERUP1", started_at=ctx.now(), is_paid=True,
                                    success=False, category="interrupted"))  # fmt: skip
        session.add(ProviderRequest(provider="scrapingdog", asin="B0SKIPPED1", started_at=ctx.now(), success=False,
                                    category="skipped_provider_quota"))  # fmt: skip
        session.commit()
    result = check_product(ctx, tracked(ctx))
    assert (result.status, result.category) == ("skipped", "provider_quota") and calls == []
    ctx.close()
