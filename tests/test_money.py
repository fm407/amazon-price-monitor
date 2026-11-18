from decimal import Decimal

import pytest

from app.money import (
    InvalidAmount,
    apply_discount,
    format_money,
    from_minor,
    parse_amount,
    percent_of,
    to_minor,
    usd_to_micro,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(579.99, "579.99"), ("1,299.00", "1299.00"), (800, "800"), (Decimal("0.1"), "0.1"), (" 12.5 ", "12.5")],
)
def test_parse_amount_accepts_valid_numbers_without_float_error(value, expected):
    assert parse_amount(value) == Decimal(expected)


@pytest.mark.parametrize(
    "value",
    [None, "", "abc", "$12", float("nan"), float("inf"), "-inf", "NaN", -1, "-0.01", True, 1e12, {}, []],
)
def test_parse_amount_rejects_invalid_values(value):
    with pytest.raises(InvalidAmount):
        parse_amount(value)


def test_minor_unit_round_trip_and_rounding():
    assert to_minor(Decimal("579.99"), "USD") == 57999
    assert to_minor(Decimal("0.005"), "USD") == 1
    assert to_minor(Decimal("1500"), "JPY") == 1500
    assert from_minor(57999, "USD") == Decimal("579.99")
    assert to_minor(parse_amount(0.1) + parse_amount(0.2), "USD") == 30


def test_format_money_shows_unknown_instead_of_zero():
    assert format_money(57999, "USD") == "$579.99"
    assert format_money(80000, "USD") == "$800.00"
    assert format_money(1500, "JPY") == "¥1,500"
    assert format_money(None, "USD") == "unknown"
    assert format_money(100, None) == "unknown"


def test_discount_target_matches_specification_example():
    assert apply_discount(80000, 2500) == 60000
    assert apply_discount(9999, 1500) == 8499  # 84.9915 rounds half-up
    assert percent_of(80000 - 57999, 80000) == Decimal("27.50")
    assert percent_of(1, 0) is None


def test_api_cost_uses_micro_dollars():
    assert usd_to_micro(Decimal("0.0015")) == 1500
