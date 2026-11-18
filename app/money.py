"""Money parsing, validation and formatting. Decimal in, integer minor units stored."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_PRICE = Decimal("10000000")
ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW"}
SYMBOLS = {"USD": "$", "CAD": "CA$", "GBP": "£", "EUR": "€", "JPY": "¥", "AUD": "A$", "MXN": "MX$"}
MICRO = Decimal(1_000_000)


class InvalidAmount(ValueError):
    pass


def exponent(currency: str) -> int:
    return 0 if currency.upper() in ZERO_DECIMAL_CURRENCIES else 2


def parse_amount(value: object) -> Decimal:
    """Convert a provider value to a finite, non-negative Decimal or raise InvalidAmount."""
    if value is None or isinstance(value, bool):
        raise InvalidAmount("amount is missing")
    if isinstance(value, str):
        value = value.strip().replace(",", "")
    try:
        # str() first so a float such as 579.99 becomes Decimal("579.99"), not its binary expansion.
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidAmount(f"not a number: {value!r}") from exc
    if not amount.is_finite():
        raise InvalidAmount("amount is not finite")
    if amount < 0:
        raise InvalidAmount("amount is negative")
    if amount > MAX_PRICE:
        raise InvalidAmount("amount is implausibly large")
    return amount


def to_minor(amount: Decimal, currency: str) -> int:
    scale = Decimal(10) ** exponent(currency)
    return int((amount * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: str) -> Decimal:
    exp = exponent(currency)
    return (Decimal(minor) / (Decimal(10) ** exp)).quantize(Decimal(1).scaleb(-exp))


def format_money(minor: int | None, currency: str | None) -> str:
    if minor is None or not currency:
        return "unknown"
    amount = from_minor(minor, currency)
    symbol = SYMBOLS.get(currency.upper())
    return f"{symbol}{amount:,}" if symbol else f"{amount:,} {currency.upper()}"


def usd_to_micro(amount: Decimal) -> int:
    return int((amount * MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def micro_to_usd(micro: int) -> Decimal:
    return (Decimal(micro) / MICRO).quantize(Decimal("0.0001"))


def percent_of(part: int, whole: int) -> Decimal | None:
    """part / whole as a percentage with two decimals. None when whole is not positive."""
    if whole <= 0:
        return None
    return (Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def apply_discount(base_minor: int, basis_points: int) -> int:
    """base * (1 - percent/100), rounded half-up to a whole minor unit."""
    result = Decimal(base_minor) * (Decimal(10000 - basis_points) / Decimal(10000))
    return int(result.quantize(Decimal(1), rounding=ROUND_HALF_UP))
