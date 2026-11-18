import json
from decimal import Decimal

import httpx
import pytest

from app.providers.base import ProviderError, parse_retry_after
from app.providers.canopy import CanopyProvider
from app.providers.mock import MockProvider

CANOPY_OK = {
    "data": {
        "amazonProduct": {
            "title": "Example GPU",
            "brand": "ASRock",
            "asin": "B0EXAMPLE1",
            "isNew": True,
            "isInStock": True,
            "price": {"symbol": "$", "value": 579.99, "currency": "USD", "display": "$579.99"},
            "recommendedRetailPrice": {"symbol": "$", "value": 800, "currency": "USD", "display": "$800.00"},
            "mainImageUrl": "https://m.media-amazon.com/images/I/example.jpg",
            "coupon": {"label": "Save $20 with coupon"},
            "seller": {"sellerId": "ATVPDKIKX0DER", "name": "Amazon.com"},
        }
    }
}


def canopy(handler) -> CanopyProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CanopyProvider(client, "https://rest.canopyapi.co", "test-key-123", Decimal("0.01"), 2)


def test_canopy_sends_documented_request_and_maps_fields():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("API-KEY")
        return httpx.Response(200, json=CANOPY_OK)

    data = canopy(handler).fetch_product("B0EXAMPLE1", "US")
    assert seen["url"] == "https://rest.canopyapi.co/api/amazon/product?asin=B0EXAMPLE1&domain=US"
    assert seen["key"] == "test-key-123"
    assert (data.price, data.currency, data.list_price) == (579.99, "USD", 800)
    assert (data.seller, data.condition, data.availability) == ("Amazon.com", "new", "in_stock")
    assert data.shipping is None  # unknown, never assumed free
    assert data.coupon_text == "Save $20 with coupon"
    assert data.price_basis == "buy_box_item_price"


def test_canopy_missing_price_seller_and_stock_stay_unknown():
    body = {"data": {"amazonProduct": {"title": "X", "price": None, "seller": None, "isInStock": False}}}
    data = canopy(lambda r: httpx.Response(200, json=body)).fetch_product("B0EXAMPLE1", "US")
    assert data.price is None
    assert data.seller is None
    assert data.availability == "out_of_stock"
    assert data.condition == "unknown"


@pytest.mark.parametrize(
    ("response", "category", "retryable"),
    [
        (httpx.Response(401, json={"success": False}), "auth", False),
        (httpx.Response(402, json={"success": False}), "quota", False),
        (httpx.Response(400, json={"success": False}), "invalid_request", False),
        (httpx.Response(404), "not_found", False),
        (httpx.Response(429, headers={"retry-after": "7"}), "rate_limited", True),
        (httpx.Response(500), "http_error", True),
        (httpx.Response(200, text="<html>oops</html>"), "malformed", False),
        (httpx.Response(200, json={"unexpected": 1}), "malformed", False),
        (httpx.Response(200, json={"data": {"amazonProduct": None}}), "not_found", False),
        (httpx.Response(200, json={"data": {"amazonProduct": ["x"]}}), "malformed", False),
    ],
)
def test_canopy_error_mapping(response, category, retryable):
    with pytest.raises(ProviderError) as info:
        canopy(lambda r: response).fetch_product("B0EXAMPLE1", "US")
    assert info.value.category == category
    assert info.value.retryable is retryable
    if category == "rate_limited":
        assert info.value.retry_after == 7
    if category in ("auth", "quota"):
        assert info.value.block_seconds > 0


def test_canopy_timeout_network_and_configuration_errors():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    for handler, category in ((timeout, "timeout"), (refused, "network")):
        with pytest.raises(ProviderError) as info:
            canopy(handler).fetch_product("B0EXAMPLE1", "US")
        assert (info.value.category, info.value.retryable) == (category, True)
    with pytest.raises(ProviderError) as info:
        canopy(lambda r: httpx.Response(200, json=CANOPY_OK)).fetch_product("B0EXAMPLE1", "ZZ")
    assert info.value.category == "unsupported_marketplace"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=CANOPY_OK)))
    with pytest.raises(ProviderError) as info:
        CanopyProvider(client, "https://rest.canopyapi.co", "", Decimal("0.01"), 1).fetch_product(
            "B0EXAMPLE1", "US"
        )
    assert info.value.category == "not_configured"


def test_canopy_error_text_never_contains_the_api_key():
    with pytest.raises(ProviderError) as info:
        canopy(lambda r: httpx.Response(401, text="bad key test-key-123")).fetch_product("B0EXAMPLE1", "US")
    assert "test-key-123" not in json.dumps([info.value.message, str(info.value)])


def test_mock_provider_is_deterministic_and_supports_simulation():
    mock = MockProvider()
    assert mock.fetch_product("B0DEMOGPU1", "US").price == "800.00"
    first = mock.fetch_product("B0ANYTHING", "US").price
    assert first == mock.fetch_product("B0ANYTHING", "US").price
    mock.set_price("B0DEMOGPU1", "599.00", shipping=None, seller="Third Party")
    data = mock.fetch_product("B0DEMOGPU1", "US")
    assert (data.price, data.shipping, data.seller) == ("599.00", None, "Third Party")
    mock.set_mode("no_price", "B0DEMOGPU1")
    assert mock.fetch_product("B0DEMOGPU1", "US").price is None
    mock.set_mode("out_of_stock", "B0DEMOGPU1")
    assert mock.fetch_product("B0DEMOGPU1", "US").availability == "out_of_stock"
    for mode, category in (("outage", "network"), ("rate_limit", "rate_limited"), ("invalid", "malformed")):
        mock.set_mode(mode)
        mock.set_mode(mode, "B0DEMOGPU1")
        with pytest.raises(ProviderError) as info:
            mock.fetch_product("B0DEMOGPU1", "US")
        assert info.value.category == category
    with pytest.raises(ValueError):
        mock.set_mode("bogus")


def test_parse_retry_after():
    assert parse_retry_after("12") == 12
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after("-5") == 0
