"""Easyparser adapter (real-time DETAIL operation).

Verified against https://docs.easyparser.com/real-time-integration/overview and
.../detail/response on 2026-11-18:
GET https://realtime.easyparser.com/v1/request?api_key=<key>&platform=AMZ&operation=DETAIL&domain=.com&asin=<ASIN>
The envelope has `request_info` (success, status_code, error_details) and `result.detail`.
`buybox_winner.price` holds value, currency (ISO code) and symbol. `condition.is_new`,
`availability.raw`, `shipping.raw` and `rrp.value` are used as documented. The response has no
seller name. The documentation says to check `request_info.success` even on HTTP 200, and that
failed requests are not charged. It recommends a client timeout of at least 60 seconds.
"""

from app.amazon import MARKETPLACES
from app.providers.base import HttpJsonProvider, ProductData, ProviderError, as_dict, as_text, availability_from_text


class EasyparserProvider(HttpJsonProvider):
    name = "easyparser"
    label = "Easyparser"
    URL = "https://realtime.easyparser.com/v1/request"
    timeout = 75.0
    reports_seller = False

    def fetch_product(self, asin: str, marketplace: str) -> ProductData:
        if marketplace not in MARKETPLACES:
            raise ProviderError("unsupported_marketplace", f"Unknown marketplace {marketplace}")
        params = {
            "api_key": self._api_key,
            "platform": "AMZ",
            "operation": "DETAIL",
            "domain": MARKETPLACES[marketplace].domain.removeprefix("amazon"),  # ".com"
            "asin": asin,
        }
        body = self.get_json(self.URL, params)
        info = as_dict(body.get("request_info"))
        detail = as_dict(as_dict(body.get("result")).get("detail"))
        if info.get("success") is False or not detail:
            if info.get("status_code") == 404:
                raise ProviderError("not_found", "Easyparser found no product for this ASIN", http_status=200)
            errors = info.get("error_details")
            first = as_dict(errors[0]) if isinstance(errors, list) and errors else {}
            message = as_text(first.get("message")) or "no product detail returned"
            raise ProviderError(
                "http_error", f"Easyparser could not read the product: {message}"[:200], http_status=200
            )
        offer = as_dict(detail.get("buybox_winner"))
        price = as_dict(offer.get("price"))
        is_new = as_dict(offer.get("condition")).get("is_new")
        shipping = as_text(as_dict(offer.get("shipping")).get("raw"))
        raw_images = detail.get("images")
        images = raw_images if isinstance(raw_images, list) else []
        main = next((as_dict(i) for i in images if as_dict(i).get("variant") == "MAIN"), {})
        return ProductData(
            asin=asin,
            marketplace=marketplace,
            title=as_text(detail.get("title")),
            brand=as_text(detail.get("brand")),
            image_url=as_text(main.get("link")),
            currency=as_text(price.get("currency")),
            price=price.get("value"),
            list_price=as_dict(offer.get("rrp")).get("value"),
            shipping="0" if shipping and shipping.upper() == "FREE" else None,
            seller=None,  # the response describes the seller type, not the seller name
            condition="unknown" if not isinstance(is_new, bool) else ("new" if is_new else "used"),
            availability=availability_from_text(as_dict(offer.get("availability")).get("raw")),
            price_basis="buy_box_item_price",
            raw={"price": price, "shipping": shipping, "availability": offer.get("availability")},
        )
