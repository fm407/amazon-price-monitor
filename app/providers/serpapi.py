"""SerpApi adapter (Amazon Product engine).

Verified against https://serpapi.com/amazon-product-api and
https://serpapi.com/api-status-and-error-codes on 2026-11-18:
GET https://serpapi.com/search?engine=amazon_product&asin=<ASIN>&amazon_domain=amazon.com&api_key=<key>
The response carries `product_results` (title, thumbnail, price, extracted_price, old_price,
extracted_old_price, stock) and `purchase_options.buy_new` (extracted_price, shipper/seller).
Errors use the documented HTTP statuses and a top-level `error` string.

The price is a display string plus a number. There is no currency code, so the currency symbol is
accepted only when it matches the marketplace currency. Delivery text is conditional ("over $35"),
so shipping stays unknown. SerpApi serves repeated searches from a one-hour cache free of charge.
"""

import re

from app.amazon import MARKETPLACES
from app.providers.base import HttpJsonProvider, ProductData, ProviderError, as_dict, as_text, availability_from_text
from app.providers.base import resolve_currency as resolve

SYMBOL_RE = re.compile(r"^\s*([^\d\s.,-]{1,3})\s*\d")


class SerpApiProvider(HttpJsonProvider):
    name = "serpapi"
    label = "SerpApi"
    URL = "https://serpapi.com/search"

    def fetch_product(self, asin: str, marketplace: str) -> ProductData:
        if marketplace not in MARKETPLACES:
            raise ProviderError("unsupported_marketplace", f"Unknown marketplace {marketplace}")
        params = {
            "engine": "amazon_product",
            "asin": asin,
            "amazon_domain": MARKETPLACES[marketplace].domain,
            "api_key": self._api_key,
        }
        body = self.get_json(self.URL, params)
        product = as_dict(body.get("product_results"))
        if not product:
            if as_dict(body.get("search_metadata")).get("status") == "Error":
                raise ProviderError("http_error", "SerpApi could not complete the search", http_status=200)
            raise ProviderError("not_found", "SerpApi returned no product for this ASIN", http_status=200)
        buy_new = as_dict(as_dict(body.get("purchase_options")).get("buy_new"))
        seller = as_dict(as_dict(buy_new.get("features")).get("shipper_seller"))
        from_new_offer = buy_new.get("extracted_price") is not None
        price_text = as_text(buy_new.get("price")) or as_text(product.get("price")) or ""
        symbol = SYMBOL_RE.match(price_text)
        return ProductData(
            asin=asin,
            marketplace=marketplace,
            title=as_text(product.get("title")),
            brand=as_text(product.get("brand")),
            image_url=as_text(product.get("thumbnail")),
            currency=resolve(symbol.group(1), marketplace) if symbol else None,
            price=buy_new.get("extracted_price") if from_new_offer else product.get("extracted_price"),
            list_price=product.get("extracted_old_price"),
            shipping=None,
            seller=as_text(seller.get("text")),
            condition="new" if from_new_offer else "unknown",
            availability=availability_from_text(buy_new.get("stock") or product.get("stock")),
            price_basis="buy_new_item_price" if from_new_offer else "listed_item_price",
            raw={key: product.get(key) for key in ("price", "extracted_price", "old_price", "stock")},
        )
