"""Canopy REST adapter.

Verified against the OpenAPI document at https://rest.canopyapi.co/api/v1/openapi.json:
GET /api/amazon/product, query `asin` and `domain`, header `API-KEY`.
Documented responses: 200, 400, 401, 402, 500. The product endpoint returns the
Buy Box price, `isNew`, `isInStock`, the seller name and a coupon label. It does
not return shipping cost, so the delivered price stays incomplete.
"""

from decimal import Decimal
from typing import Any

import httpx

from app.providers.base import AUTH_BLOCK_SECONDS, ProductData, ProviderError, parse_retry_after

DOMAINS = {
    "US",
    "UK",
    "CA",
    "DE",
    "FR",
    "IT",
    "ES",
    "AU",
    "IN",
    "MX",
    "BR",
    "JP",
    "PL",
    "AE",
    "SG",
    "NL",
    "SE",
}
RAW_KEYS = ("asin", "price", "recommendedRetailPrice", "isNew", "isInStock", "isPrime", "seller", "coupon")


class CanopyProvider:
    name = "canopy"
    is_paid = True

    def __init__(
        self,
        client: httpx.Client,
        base_url: str,
        api_key: str,
        cost_per_request_usd: Decimal,
        max_concurrency: int,
    ) -> None:
        self._client = client
        self._url = base_url.rstrip("/") + "/api/amazon/product"
        self._api_key = api_key
        self.cost_per_request_usd = cost_per_request_usd
        self.max_concurrency = max_concurrency

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def fetch_product(self, asin: str, marketplace: str) -> ProductData:
        if not self.configured:
            raise ProviderError("not_configured", "CANOPY_API_KEY is not set")
        if marketplace not in DOMAINS:
            raise ProviderError("unsupported_marketplace", f"Canopy does not support marketplace {marketplace}")
        try:
            response = self._client.get(
                self._url, params={"asin": asin, "domain": marketplace}, headers={"API-KEY": self._api_key}
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "Canopy request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network", f"Canopy network error: {type(exc).__name__}", retryable=True) from exc
        self._raise_for_status(response)
        try:
            body = response.json()
            product = body["data"]["amazonProduct"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("malformed", "Canopy returned an unexpected response", http_status=200) from exc
        if product is None:
            raise ProviderError("not_found", "Canopy found no product for this ASIN", http_status=200)
        if not isinstance(product, dict):
            raise ProviderError("malformed", "Canopy returned an unexpected product object", http_status=200)
        return self._to_product_data(asin, marketplace, product)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status == 200:
            return
        if status in (401, 403):
            raise ProviderError(
                "auth", "Canopy rejected the API key", http_status=status, block_seconds=AUTH_BLOCK_SECONDS
            )
        if status == 402:
            raise ProviderError(
                "quota",
                "Canopy plan quota or payment required",
                http_status=status,
                block_seconds=AUTH_BLOCK_SECONDS,
            )
        if status == 404:
            raise ProviderError("not_found", "Canopy found no product for this ASIN", http_status=status)
        if status == 400:
            raise ProviderError("invalid_request", "Canopy rejected the request parameters", http_status=status)
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            raise ProviderError(
                "rate_limited",
                "Canopy rate limit reached",
                retryable=True,
                http_status=status,
                retry_after=retry_after,
            )
        raise ProviderError("http_error", f"Canopy returned HTTP {status}", retryable=status >= 500, http_status=status)

    @staticmethod
    def _to_product_data(asin: str, marketplace: str, product: dict[str, Any]) -> ProductData:
        def obj(key: str) -> dict[str, Any]:
            value = product.get(key)
            return value if isinstance(value, dict) else {}

        price, seller, coupon = obj("price"), obj("seller"), obj("coupon")
        in_stock, is_new = product.get("isInStock"), product.get("isNew")
        return ProductData(
            asin=asin,
            marketplace=marketplace,
            title=product.get("title") or None,
            brand=product.get("brand") or None,
            image_url=product.get("mainImageUrl") or None,
            currency=price.get("currency") or None,
            price=price.get("value"),
            list_price=obj("recommendedRetailPrice").get("value"),
            shipping=None,  # not part of the product endpoint
            seller=seller.get("name") or None,
            condition="unknown" if is_new is None else ("new" if is_new else "used"),
            availability="unknown" if in_stock is None else ("in_stock" if in_stock else "out_of_stock"),
            coupon_text=coupon.get("label") or None,
            price_basis="buy_box_item_price",
            raw={key: product.get(key) for key in RAW_KEYS},
        )
