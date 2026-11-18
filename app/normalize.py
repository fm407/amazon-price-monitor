"""Price normalization: decide whether a provider response holds a valid, eligible, monitored price."""

from dataclasses import dataclass

from app.money import InvalidAmount, parse_amount, to_minor
from app.providers.base import ProductData

USED_SELLER_MARKERS = ("warehouse", "resale", "renewed")


class PriceRejected(Exception):
    """The response is well-formed but yields no price that may be stored or alerted on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class NormalizedPrice:
    monitored_minor: int  # delivered price when shipping is known, item price otherwise
    item_minor: int
    shipping_minor: int | None
    effective_minor: int | None
    list_minor: int | None
    currency: str
    price_basis: str  # delivered | item_only (shipping unknown)


def is_amazon_seller(seller: str | None) -> bool:
    if not seller:
        return False
    name = seller.strip().lower()
    return name.startswith("amazon") and not any(marker in name for marker in USED_SELLER_MARKERS)


def _optional_minor(value: object, currency: str) -> int | None:
    try:
        return to_minor(parse_amount(value), currency)
    except InvalidAmount:
        return None


def normalize(
    data: ProductData,
    *,
    expected_currency: str | None,
    seller_filter: str = "any",
    include_used: bool = False,
) -> NormalizedPrice:
    if data.availability == "out_of_stock":
        raise PriceRejected("out_of_stock", "The product is out of stock.")
    if data.price is None:
        raise PriceRejected("no_price", "The provider returned no price.")
    try:
        amount = parse_amount(data.price)
    except InvalidAmount as exc:
        raise PriceRejected("invalid_price", f"The provider returned an invalid price ({exc}).") from exc
    if amount == 0:
        # A zero price means "unavailable" far more often than "free". Never treat it as a deal.
        raise PriceRejected("zero_price", "The provider returned a zero price.")
    currency = (data.currency or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise PriceRejected("no_currency", "The provider returned no valid currency.")
    if expected_currency and currency != expected_currency:
        raise PriceRejected(
            "currency_mismatch", f"Price is in {currency}; this product is tracked in {expected_currency}."
        )
    if data.condition == "used" and not include_used:
        raise PriceRejected("condition_excluded", "The current offer is used or refurbished.")
    if seller_filter == "amazon" and not is_amazon_seller(data.seller):
        raise PriceRejected("seller_excluded", "The current offer is not sold by Amazon.")

    item_minor = to_minor(amount, currency)
    if item_minor <= 0:
        raise PriceRejected("zero_price", "The provider returned a price below one minor unit.")
    shipping_minor = None if data.shipping is None else _optional_minor(data.shipping, currency)
    effective_minor = None if shipping_minor is None else item_minor + shipping_minor
    return NormalizedPrice(
        monitored_minor=item_minor if effective_minor is None else effective_minor,
        item_minor=item_minor,
        shipping_minor=shipping_minor,
        effective_minor=effective_minor,
        list_minor=None if data.list_price is None else _optional_minor(data.list_price, currency),
        currency=currency,
        price_basis="item_only" if effective_minor is None else "delivered",
    )
