"""Provider contract. Adapters keep provider-specific parsing; validation happens in app.normalize."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import httpx

from app.amazon import MARKETPLACES


@dataclass(frozen=True)
class ProductData:
    """Normalized shape of one provider response. Price fields are raw and unvalidated."""

    asin: str
    marketplace: str
    title: str | None = None
    brand: str | None = None
    image_url: str | None = None
    currency: str | None = None
    price: Any = None  # current selling price of the selected offer; None when absent
    list_price: Any = None  # reference/list price, never used as the monitored price
    shipping: Any = None  # None = unknown; 0 only when the provider states free shipping
    seller: str | None = None
    condition: str = "unknown"  # new | used | unknown
    availability: str = "unknown"  # in_stock | out_of_stock | unknown
    coupon_text: str | None = None
    price_basis: str = "current_offer"
    raw: dict[str, Any] = field(default_factory=dict)
    http_status: int | None = 200
    actual_cost_usd: Decimal | None = None


class ProviderError(Exception):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        retry_after: float | None = None,
        block_seconds: float = 0,
        actual_cost_usd: Decimal | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after = retry_after
        self.block_seconds = block_seconds  # pause every request to this provider for this long
        self.actual_cost_usd = actual_cost_usd


class PriceProvider(Protocol):
    name: str
    is_paid: bool
    cost_per_request_usd: Decimal
    max_concurrency: int
    # Optional per-provider monthly limits, for example to stay inside a free plan.
    max_monthly_requests: int | None
    max_monthly_spend_usd: Decimal | None
    reports_seller: bool  # False when the provider never names the seller

    @property
    def configured(self) -> bool: ...

    def fetch_product(self, asin: str, marketplace: str) -> ProductData: ...


AUTH_BLOCK_SECONDS = 3600.0
MAX_RETRY_AFTER_SECONDS = 86400.0


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header. HTTP-date values are ignored (treated as absent)."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


# Some sources report a currency symbol instead of an ISO code. A symbol is accepted only when it is
# the symbol of the marketplace's own currency; anything else stays unknown and the price is rejected.
SYMBOL_CURRENCIES = {"$": {"USD", "CAD", "AUD", "MXN"}, "£": {"GBP"}, "€": {"EUR"}, "¥": {"JPY"}}


def resolve_currency(value: str | None, marketplace: str) -> str | None:
    if value and value in SYMBOL_CURRENCIES:
        expected = MARKETPLACES[marketplace].currency
        return expected if expected in SYMBOL_CURRENCIES[value] else None
    return value


def availability_from_text(text: object) -> str:
    """Map a stock phrase such as "In Stock" or "Currently unavailable" to an availability value."""
    if not isinstance(text, str) or not text.strip():
        return "unknown"
    phrase = text.lower()
    if "unavailable" in phrase or "out of stock" in phrase:
        return "out_of_stock"
    return "in_stock" if "in stock" in phrase else "unknown"


def as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class HttpJsonProvider:
    """Shared behaviour for providers that answer one GET request with one JSON document."""

    name = "http"
    label = "Provider"
    is_paid = True
    max_concurrency = 2
    max_monthly_requests: int | None = None
    max_monthly_spend_usd: Decimal | None = None
    reports_seller = True
    timeout: float | None = None  # None = the shared client's timeout

    def __init__(self, client: httpx.Client, api_key: str, cost_per_request_usd: Decimal) -> None:
        self._client = client
        self._api_key = api_key
        self.cost_per_request_usd = cost_per_request_usd

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        """GET and decode. The API key travels in the query string, so URLs are never put in errors."""
        if not self.configured:
            raise ProviderError("not_configured", f"The {self.label} API key is not set")
        options: dict[str, Any] = {} if self.timeout is None else {"timeout": self.timeout}
        try:
            response = self._client.get(url, params=params, **options)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"{self.label} request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network", f"{self.label} network error: {type(exc).__name__}", retryable=True) from exc
        status = response.status_code
        if status in (401, 403):
            raise ProviderError(
                "auth", f"{self.label} rejected the API key", http_status=status, block_seconds=AUTH_BLOCK_SECONDS
            )
        if status == 402:
            raise ProviderError(
                "quota", f"{self.label} plan credits are used up", http_status=status, block_seconds=AUTH_BLOCK_SECONDS
            )
        if status == 404:
            raise ProviderError("not_found", f"{self.label} found no product for this ASIN", http_status=status)
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            raise ProviderError(
                "rate_limited",
                f"{self.label} rate limit or plan limit reached",
                retryable=True,
                http_status=status,
                retry_after=retry_after,
            )
        if status != 200:
            raise ProviderError(
                "invalid_request" if status == 400 else "http_error",
                f"{self.label} returned HTTP {status}",
                retryable=status >= 500,
                http_status=status,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("malformed", f"{self.label} returned a non-JSON response", http_status=200) from exc
        if not isinstance(body, dict):
            raise ProviderError("malformed", f"{self.label} returned an unexpected response", http_status=200)
        return body
