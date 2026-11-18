"""Provider contract. Adapters keep provider-specific parsing; validation happens in app.normalize."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol


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
