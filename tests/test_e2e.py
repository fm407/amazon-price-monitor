"""End-to-end alert scenarios through the real pipeline with the mock provider and mock channel."""

from sqlalchemy import select

from app import alerts, notify, products
from app.checker import check_product
from app.models import (
    FIXED_TARGET,
    NEW_LOW,
    PCT_BELOW_AVERAGE,
    PCT_BELOW_BASELINE,
    PCT_DROP_SINCE_LAST,
    AlertEvent,
    AlertRule,
    NotificationDelivery,
    Product,
)
from tests.conftest import GPU, make_ctx


def add_with_rule(ctx, kind=PCT_BELOW_BASELINE, threshold="25", asin=GPU, **options) -> Product:
    with ctx.sessions() as session:
        product, _ = products.add_product(ctx, session, asin)
        alerts.create_rule(session, product, kind, threshold, **options)
        session.commit()
        return product


def step(ctx, mock, product, price, hours=6, asin=GPU, **fields):
    ctx.now.advance(hours=hours)
    mock.set_price(asin, price, **fields)
    return check_product(ctx, product.id)


def events(ctx) -> list[AlertEvent]:
    with ctx.sessions() as session:
        return list(session.scalars(select(AlertEvent).order_by(AlertEvent.id)))


def deliveries(ctx) -> list[NotificationDelivery]:
    with ctx.sessions() as session:
        return list(session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id)))


def sent(ctx) -> list[str]:
    return ctx.notifiers["mock"].sent


def test_specification_scenario_including_restart(ctx, mock, settings):
    product = add_with_rule(ctx)  # mock price $800, 25% discount rule -> target $600
    assert product.baseline_minor == 80000

    step(ctx, mock, product, "700.00")
    assert sent(ctx) == []

    step(ctx, mock, product, "599.00")
    assert len(sent(ctx)) == 1
    body = sent(ctx)[0]
    for expected in (
        "PRICE DROP ALERT", "Previous reference price: $800.00", "Current price: $599.00", "Discount: 25.13%",
        "Target price: $600.00", "Savings: $201.00", "Seller: Amazon.com", "Availability: In stock",
        "Provider: Mock", "Rule: 25% below baseline", "fictional demonstration price",
    ):  # fmt: skip
        assert expected in body, expected

    step(ctx, mock, product, "599.00")
    assert len(sent(ctx)) == 1  # still below: no duplicate

    step(ctx, mock, product, "750.00")
    assert len(sent(ctx)) == 1  # recovery sends nothing but re-arms the rule

    step(ctx, mock, product, "590.00")
    assert len(sent(ctx)) == 2
    assert [(e.event_type, e.price_minor, e.target_minor) for e in events(ctx)] == [
        ("trigger", 59900, 60000),
        ("trigger", 59000, 60000),
    ]
    assert [(d.channel, d.status, d.attempts) for d in deliveries(ctx)] == [("mock", "sent", 1)] * 2
    ctx.close()

    # Restart: a new process on the same database. Alert state must persist.
    restarted = make_ctx(settings)
    restarted.now.value = ctx.now()
    new_mock = restarted.providers["mock"]
    step(restarted, new_mock, product, "590.00")
    assert sent(restarted) == [] and len(events(restarted)) == 2  # still below after restart: no duplicate
    step(restarted, new_mock, product, "800.00")
    step(restarted, new_mock, product, "580.00")
    assert len(sent(restarted)) == 1 and len(events(restarted)) == 3
    restarted.close()


def test_price_exactly_at_target_triggers(ctx, mock):
    product = add_with_rule(ctx)
    step(ctx, mock, product, "600.01")
    assert sent(ctx) == []
    step(ctx, mock, product, "600.00")
    assert len(sent(ctx)) == 1


def test_fixed_target_alert(ctx, mock):
    product = add_with_rule(ctx, FIXED_TARGET, "550")
    step(ctx, mock, product, "551.00")
    assert sent(ctx) == []
    step(ctx, mock, product, "549.99")
    assert len(sent(ctx)) == 1 and "Target price: $550.00" in sent(ctx)[0]
    step(ctx, mock, product, "540.00")
    assert len(sent(ctx)) == 1


def test_new_historical_low_alert(ctx, mock):
    product = add_with_rule(ctx, NEW_LOW, None, cooldown_minutes=0)
    step(ctx, mock, product, "820.00")
    step(ctx, mock, product, "800.00")  # equals the previous low: not a new low
    assert sent(ctx) == []
    step(ctx, mock, product, "790.00")
    step(ctx, mock, product, "795.00")
    step(ctx, mock, product, "780.00")
    assert [e.price_minor for e in events(ctx)] == [79000, 78000]
    assert [e.reference_minor for e in events(ctx)] == [80000, 79000]


def test_percent_drop_since_last_valid_observation(ctx, mock):
    product = add_with_rule(ctx, PCT_DROP_SINCE_LAST, "15", cooldown_minutes=0)
    step(ctx, mock, product, "700.00")  # 12.5% drop
    assert sent(ctx) == []
    mock.set_mode("no_price", GPU)
    check_product(ctx, product.id)  # invalid observation in between is ignored
    mock.set_mode("ok", GPU)
    step(ctx, mock, product, "595.00")  # exactly 15% below the last valid price
    assert len(sent(ctx)) == 1 and "Previous reference price: $700.00" in sent(ctx)[0]


def test_average_rule_waits_for_enough_history(ctx, mock):
    product = add_with_rule(ctx, PCT_BELOW_AVERAGE, "10", lookback_days=30)
    for _ in range(3):
        step(ctx, mock, product, "800.00")
    step(ctx, mock, product, "600.00")  # only 4 earlier observations: rule not active yet
    assert sent(ctx) == []
    step(ctx, mock, product, "800.00")
    step(ctx, mock, product, "690.00")  # average of 6 earlier observations is $766.67; target $690.00
    assert len(sent(ctx)) == 1 and "Previous reference price: $766.67" in sent(ctx)[0]


def test_invalid_observations_never_trigger_or_reset_alerts(ctx, mock):
    product = add_with_rule(ctx)
    step(ctx, mock, product, "599.00")
    assert len(sent(ctx)) == 1
    for mode in ("no_price", "out_of_stock", "invalid", "outage"):
        mock.set_mode(mode, GPU)
        ctx.now.advance(hours=6)
        check_product(ctx, product.id)
    mock.set_mode("ok", GPU)
    step(ctx, mock, product, 0)
    step(ctx, mock, product, "599.00")
    assert len(sent(ctx)) == 1 and len(events(ctx)) == 1  # the rule never saw a recovery


def test_disabled_rule_and_baseline_change(ctx, mock):
    product = add_with_rule(ctx)
    with ctx.sessions() as session:
        rule = session.scalar(select(AlertRule))
        alerts.update_rule(rule, session.get(Product, product.id), enabled=False)
        session.commit()
    step(ctx, mock, product, "500.00")
    assert sent(ctx) == []
    with ctx.sessions() as session:
        stored = session.get(Product, product.id)
        alerts.update_rule(session.scalar(select(AlertRule)), stored, enabled=True)
        products.set_baseline(stored, 60000)  # manual baseline of $600 -> target $450
        session.commit()
    step(ctx, mock, product, "480.00")
    assert sent(ctx) == []
    step(ctx, mock, product, "450.00")
    assert len(sent(ctx)) == 1 and "Target price: $450.00" in sent(ctx)[0]


def test_reminder_and_further_drop_end_to_end(ctx, mock):
    product = add_with_rule(ctx, reminder_minutes=720, further_drop_percent="5")
    step(ctx, mock, product, "599.00")
    step(ctx, mock, product, "599.00")  # 6 hours later: nothing
    step(ctx, mock, product, "599.00")  # 12 hours later: reminder
    step(ctx, mock, product, "560.00")  # more than 5% below $599: further drop
    assert [e.event_type for e in events(ctx)] == ["trigger", "reminder", "further_drop"]
    assert [b.splitlines()[0] for b in sent(ctx)] == ["PRICE DROP ALERT", "PRICE STILL LOW", "PRICE DROPPED FURTHER"]


def test_events_are_persisted_even_when_no_channel_is_enabled(ctx, mock):
    ctx.notifiers.clear()
    product = add_with_rule(ctx)
    step(ctx, mock, product, "599.00")
    assert len(events(ctx)) == 1 and deliveries(ctx) == []
    assert notify.deliver_pending(ctx) == 0
