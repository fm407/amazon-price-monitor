"""Deterministic mock provider for development, demos and tests. All prices are fictional."""

import threading
import zlib
from decimal import Decimal
from typing import Any

from app.providers.base import ProductData, ProviderError

DEMO_IMAGE = "/static/img/demo-product.svg"
# asin -> (title, brand, fictional starting price)
CATALOG: dict[str, tuple[str, str, str]] = {
    "B0DEMOGPU1": ("ASRock Intel Arc Pro B60 24GB Graphics Card [DEMO]", "ASRock", "800.00"),
    "B0DEMOSSD1": ("4TB NVMe M.2 Internal SSD [DEMO]", "DemoStor", "260.00"),
    "B0DEMORAM1": ("64GB (2x32GB) DDR4-3200 Memory Kit [DEMO]", "DemoMem", "140.00"),
}
MODES = ("ok", "outage", "rate_limit", "invalid", "no_price", "out_of_stock", "not_found", "auth")


class MockProvider:
    name = "mock"
    is_paid = False
    cost_per_request_usd = Decimal(0)
    max_concurrency = 4
    configured = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._overrides: dict[str, dict[str, Any]] = {}
        self._modes: dict[str, str] = {}
        self.calls = 0

    def set_price(self, asin: str, price: Any, **fields: Any) -> None:
        """Simulate a price change. Extra fields: shipping, seller, condition, currency."""
        with self._lock:
            self._overrides.setdefault(asin, {}).update({"price": price, **fields})

    def set_mode(self, mode: str, asin: str = "*") -> None:
        """Simulate a failure mode for one ASIN, or for every ASIN with asin='*'."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        with self._lock:
            self._modes[asin] = mode

    def _default(self, asin: str) -> tuple[str, str, str]:
        if asin in CATALOG:
            return CATALOG[asin]
        cents = 2000 + zlib.crc32(asin.encode()) % 48000
        return (f"Mock product {asin} [MOCK]", "MockBrand", f"{cents // 100}.{cents % 100:02d}")

    def fetch_product(self, asin: str, marketplace: str) -> ProductData:
        with self._lock:
            self.calls += 1
            mode = self._modes.get(asin) or self._modes.get("*", "ok")
            fields = dict(self._overrides.get(asin, {}))
        if mode == "outage":
            raise ProviderError("network", "Simulated provider outage", retryable=True)
        if mode == "rate_limit":
            raise ProviderError(
                "rate_limited", "Simulated rate limit", retryable=True, http_status=429, retry_after=1
            )
        if mode == "auth":
            raise ProviderError("auth", "Simulated invalid API key", http_status=401, block_seconds=60)
        if mode == "not_found":
            raise ProviderError("not_found", "Simulated missing product", http_status=404)
        if mode == "invalid":
            raise ProviderError("malformed", "Simulated malformed response", http_status=200)
        title, brand, default_price = self._default(asin)
        return ProductData(
            asin=asin,
            marketplace=marketplace,
            title=title,
            brand=brand,
            image_url=DEMO_IMAGE,
            currency=fields.get("currency", "USD"),
            price=None if mode == "no_price" else fields.get("price", default_price),
            list_price=default_price,
            shipping=fields.get("shipping", "0"),
            seller=fields.get("seller", "Amazon.com"),
            condition=fields.get("condition", "new"),
            availability="out_of_stock" if mode == "out_of_stock" else "in_stock",
            price_basis="mock_fictional_price",
            raw={"mock": True, "mode": mode},
        )
