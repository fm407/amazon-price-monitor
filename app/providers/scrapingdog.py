"""Scrapingdog adapter (Amazon Product API).

Verified against https://www.scrapingdog.com/documentation/amazon-product-scraper/ on 2026-11-18:
GET https://api.scrapingdog.com/amazon/product?api_key=<key>&asin=<ASIN>&domain=com&country=us
Fields used: title, brand, main_image, price_symbol, previous_price, availability_status, sold_by
and purchase_options.single_offer.extracted_price. The top-level `price` is a display string such
as "$1,849.99 with 7 percent savings"; it is used only when the structured price is absent and the
string starts with the currency symbol and a number.

There is no currency code, so the symbol is accepted only when it matches the marketplace currency.
The page documents no error statuses; the shared HTTP handling applies. `shipping_info` is free
text, so shipping stays unknown.
"""

import re

from app.amazon import MARKETPLACES
from app.providers.base import HttpJsonProvider, ProductData, ProviderError, as_dict, as_text, availability_from_text
from app.providers.base import resolve_currency as resolve

NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def leading_amount(text: object, symbol: str | None) -> str | None:
    """The number right after the currency symbol at the start of a display string, or None."""
    if not isinstance(text, str) or not symbol or not text.strip().startswith(symbol):
        return None
    match = NUMBER_RE.match(text.strip()[len(symbol) :].lstrip())
    return match.group(0).replace(",", "") if match else None


class ScrapingdogProvider(HttpJsonProvider):
    name = "scrapingdog"
    label = "Scrapingdog"
    URL = "https://api.scrapingdog.com/amazon/product"

    def fetch_product(self, asin: str, marketplace: str) -> ProductData:
        if marketplace not in MARKETPLACES:
            raise ProviderError("unsupported_marketplace", f"Unknown marketplace {marketplace}")
        params = {
            "api_key": self._api_key,
            "asin": asin,
            "domain": MARKETPLACES[marketplace].domain.removeprefix("amazon."),  # "com"
            "country": marketplace.lower(),
        }
        body = self.get_json(self.URL, params)
        if not as_text(body.get("title")):
            raise ProviderError("not_found", "Scrapingdog returned no product for this ASIN", http_status=200)
        symbol = as_text(body.get("price_symbol"))
        offer = as_dict(as_dict(body.get("purchase_options")).get("single_offer"))
        structured = offer.get("extracted_price")
        return ProductData(
            asin=asin,
            marketplace=marketplace,
            title=as_text(body.get("title")),
            brand=as_text(body.get("brand")),
            image_url=as_text(body.get("main_image")),
            currency=resolve(symbol, marketplace),
            price=structured if structured is not None else leading_amount(body.get("price"), symbol),
            list_price=leading_amount(body.get("previous_price"), symbol),
            shipping=None,
            seller=as_text(body.get("sold_by")),
            condition="unknown",
            availability=availability_from_text(body.get("availability_status")),
            price_basis="single_offer_item_price" if structured is not None else "listed_item_price",
            raw={key: body.get(key) for key in ("price", "price_symbol", "availability_status", "sold_by")},
        )
