"""Unit tests for threshold maths and alert state transitions."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.alerts import InvalidRule, decide, describe, encode_threshold, update_rule
from app.models import FIXED_TARGET, NEW_LOW, PCT_BELOW_BASELINE, PCT_DROP_SINCE_LAST, AlertRule, Product
from tests.conftest import T0


def rule(kind=PCT_BELOW_BASELINE, **fields) -> AlertRule:
    defaults = {"threshold": 2500, "cooldown_minutes": 60, "is_below": False, "lookback_days": 30, "channels": []}
    return AlertRule(kind=kind, **{**defaults, **fields})


def test_threshold_encoding():
    assert encode_threshold(PCT_BELOW_BASELINE, "25", "USD") == 2500
    assert encode_threshold(PCT_BELOW_BASELINE, Decimal("12.5"), "USD") == 1250
    assert encode_threshold(FIXED_TARGET, "550", "USD") == 55000
    assert encode_threshold(FIXED_TARGET, "550", "JPY") == 550
    assert encode_threshold(NEW_LOW, None, "USD") == 0


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (PCT_BELOW_BASELINE, 0), (PCT_BELOW_BASELINE, 100), (PCT_BELOW_BASELINE, -5), (PCT_BELOW_BASELINE, "x"),
        (PCT_BELOW_BASELINE, None), (PCT_BELOW_BASELINE, float("nan")), (FIXED_TARGET, 0), (FIXED_TARGET, "inf"),
        ("bogus", 5),
    ],
)  # fmt: skip
def test_invalid_thresholds_are_rejected(kind, value):
    with pytest.raises(InvalidRule):
        encode_threshold(kind, value, "USD")


def test_rule_descriptions():
    assert describe(rule(), "USD") == "25% below baseline"
    assert describe(rule(threshold=1250), "USD") == "12.5% below baseline"
    assert describe(rule(FIXED_TARGET, threshold=55000), "USD") == "At or below $550.00"
    assert describe(rule(NEW_LOW), "USD") == "New historical low"


def test_crossing_triggers_once_then_stays_quiet_until_reset():
    r = rule()
    assert decide(r, False, 70000, T0) == (None, False)
    event, r.is_below = decide(r, True, 59900, T0)
    assert (event, r.is_below) == ("trigger", True)
    r.last_triggered_at, r.last_trigger_price_minor = T0, 59900
    assert decide(r, True, 59900, T0 + timedelta(hours=6)) == (None, True)  # still below: no duplicate
    assert decide(r, False, 75000, T0 + timedelta(hours=12)) == (None, False)  # recovery resets eligibility
    r.is_below = False
    assert decide(r, True, 59000, T0 + timedelta(hours=18)) == ("trigger", True)


def test_cooldown_delays_a_new_crossing_without_losing_it():
    r = rule(cooldown_minutes=120, last_triggered_at=T0, last_trigger_price_minor=59900)
    assert decide(r, True, 59000, T0 + timedelta(minutes=30)) == (None, False)  # stays armed
    assert decide(r, True, 59000, T0 + timedelta(minutes=121)) == ("trigger", True)


def test_reminder_policy():
    r = rule(is_below=True, last_triggered_at=T0, last_trigger_price_minor=59900, reminder_minutes=1440)
    assert decide(r, True, 59900, T0 + timedelta(hours=23)) == (None, True)
    assert decide(r, True, 59900, T0 + timedelta(hours=25)) == ("reminder", True)
    r.reminder_minutes = None
    assert decide(r, True, 59900, T0 + timedelta(days=30)) == (None, True)


def test_further_drop_policy():
    r = rule(is_below=True, last_triggered_at=T0, last_trigger_price_minor=60000, further_drop_bp=500)
    later = T0 + timedelta(hours=6)
    assert decide(r, True, 58000, later) == (None, True)  # 3.3% lower: not enough
    assert decide(r, True, 57000, later) == ("further_drop", True)  # exactly 5% lower
    assert decide(r, True, 50000, T0 + timedelta(minutes=10)) == (None, True)  # inside the cooldown


def test_event_rules_fire_per_occurrence_and_respect_cooldown():
    for kind in (NEW_LOW, PCT_DROP_SINCE_LAST):
        r = rule(kind)
        assert decide(r, True, 50000, T0) == ("trigger", False)
        r.last_triggered_at = T0
        assert decide(r, True, 49000, T0 + timedelta(minutes=5)) == (None, False)
        assert decide(r, True, 49000, T0 + timedelta(hours=2)) == ("trigger", False)
        assert decide(r, False, 60000, T0 + timedelta(hours=3)) == (None, False)


def test_update_rule_validates_options_and_resets_state_on_new_threshold():
    product = Product(asin="B0DEMOGPU1", marketplace="US", currency="USD", title="x", url="x")
    r = rule(is_below=True)
    update_rule(
        r,
        product,
        threshold="30",
        cooldown_minutes=0,
        reminder_minutes="",
        further_drop_percent="5",
        channels=["discord", "discord"],
    )
    assert (r.threshold, r.is_below, r.cooldown_minutes, r.reminder_minutes, r.further_drop_bp) == (
        3000,
        False,
        0,
        None,
        500,
    )
    assert r.channels == ["discord"]
    for bad in (
        {"channels": ["sms"]},
        {"cooldown_minutes": -1},
        {"reminder_minutes": 3},
        {"further_drop_percent": 150},
        {"lookback_days": 0},
    ):
        with pytest.raises(InvalidRule):
            update_rule(r, product, **bad)
