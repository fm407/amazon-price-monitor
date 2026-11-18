"""Notification delivery against a mocked Discord webhook."""

import json
import logging
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import alerts, notify, products
from app.checker import check_product
from app.config import Settings
from app.logging_config import JsonFormatter, configure_logging
from app.models import NotificationDelivery
from app.notify import DiscordNotifier, EmailNotifier, NotifyError
from tests.conftest import GPU, make_ctx

WEBHOOK = "https://discord.com/api/webhooks/123456/s3cretTokenValue"


def discord_ctx(tmp_path, responses: list[httpx.Response], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0) if len(responses) > 1 else responses[0]

    settings = Settings(
        _env_file=None, data_dir=tmp_path, scheduler_enabled=False, log_level="WARNING",
        discord_webhook_url=WEBHOOK, notify_mock_enabled=False,
    )  # fmt: skip
    return make_ctx(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def multipart_parts(request: httpx.Request) -> tuple[dict, bytes | None]:
    """(payload_json, uploaded chart) of a request. A plain JSON request has no chart."""
    content_type = request.headers["content-type"]
    if content_type.startswith("application/json"):
        return json.loads(request.content), None
    from email import message_from_bytes

    message = message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content)
    parts = {part.get_param("name", header="content-disposition"): part for part in message.get_payload()}
    upload = parts["files[0]"]
    assert upload.get_filename() == "price-history.png" and upload.get_content_type() == "image/png"
    return json.loads(parts["payload_json"].get_payload(decode=True)), upload.get_payload(decode=True)


def trigger_alert(ctx, title_price="599.00"):
    with ctx.sessions() as session:
        product, _ = products.add_product(ctx, session, GPU)
        alerts.create_rule(session, product, "pct_below_baseline", "25")
        session.commit()
    ctx.now.advance(hours=6)
    ctx.providers["mock"].set_price(GPU, title_price)
    check_product(ctx, product.id)
    return product


def deliveries(ctx) -> list[NotificationDelivery]:
    with ctx.sessions() as session:
        return list(session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id)))


def test_discord_alert_is_delivered_with_documented_payload(tmp_path):
    seen: list[httpx.Request] = []
    ctx = discord_ctx(tmp_path, [httpx.Response(200, json={"id": "1"})], seen)
    trigger_alert(ctx)
    [request] = seen
    assert str(request.url) == WEBHOOK + "?wait=true"
    payload, chart = multipart_parts(request)
    assert chart is not None and chart.startswith(PNG_SIGNATURE)
    assert payload["attachments"] == [{"id": 0, "filename": "price-history.png"}]
    assert payload["embeds"][0]["image"] == {"url": "attachment://price-history.png"}
    assert payload["allowed_mentions"] == {"parse": []}
    [embed] = payload["embeds"]
    assert embed["title"] == "PRICE DROP ALERT"
    assert embed["url"] == "https://www.amazon.com/dp/B0DEMOGPU1"
    assert "Current price: $599.00" in embed["description"]
    assert "thumbnail" not in embed  # the demo image is a local path, not a public https URL
    [delivery] = deliveries(ctx)
    assert (delivery.channel, delivery.status, delivery.attempts) == ("discord", "sent", 1)


def test_discord_markdown_and_mentions_are_neutralised():
    delivery = NotificationDelivery(
        channel="discord", subject="s", link_url=None, image_url="https://m.media-amazon.com/x.jpg",
        body="PRICE DROP ALERT\n\nProduct: **Bold** _GPU_ [link](http://evil) @everyone `code`",
    )  # fmt: skip
    payload = DiscordNotifier.payload(delivery)
    description = payload["embeds"][0]["description"]
    assert r"\*\*Bold\*\*" in description and r"\_GPU\_" in description and r"\[link\]\(http://evil\)" in description
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["embeds"][0]["thumbnail"] == {"url": "https://m.media-amazon.com/x.jpg"}
    long = NotificationDelivery(channel="discord", subject="s", body="H" * 400 + "\n" + "x" * 9000)
    embed = DiscordNotifier.payload(long)["embeds"][0]
    assert len(embed["title"]) == 256 and len(embed["description"]) == 4096


def test_rate_limit_and_server_errors_are_retried_with_backoff(tmp_path):
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(429, json={"message": "You are being rate limited.", "retry_after": 300.5}),
        httpx.Response(503),
        httpx.Response(200, json={"id": "1"}),
    ]
    ctx = discord_ctx(tmp_path, responses, seen)
    trigger_alert(ctx)
    [delivery] = deliveries(ctx)
    assert (delivery.status, delivery.attempts) == ("retrying", 1)
    assert delivery.next_attempt_at == ctx.now() + timedelta(seconds=300.5)  # Discord's retry_after is honoured
    assert notify.deliver_pending(ctx) == 0  # not due yet
    ctx.now.advance(seconds=301)
    notify.deliver_pending(ctx)
    [delivery] = deliveries(ctx)
    assert (delivery.status, delivery.attempts) == ("retrying", 2) and "503" in delivery.last_error
    ctx.now.advance(minutes=5)
    notify.deliver_pending(ctx)
    [delivery] = deliveries(ctx)
    assert (delivery.status, delivery.attempts, delivery.last_error) == ("sent", 3, None)
    assert len(seen) == 3


def test_permanent_failure_is_not_retried_and_never_leaks_the_webhook_token(tmp_path, capsys):
    seen: list[httpx.Request] = []
    ctx = discord_ctx(tmp_path, [httpx.Response(404, json={"message": f"Unknown Webhook {WEBHOOK}"})], seen)
    configure_logging("INFO", ctx.settings.secret_values())
    logging.getLogger().handlers[0].setFormatter(JsonFormatter())
    trigger_alert(ctx)
    ctx.now.advance(days=1)
    notify.deliver_pending(ctx)
    [delivery] = deliveries(ctx)
    assert (delivery.status, delivery.attempts) == ("failed", 1) and len(seen) == 1
    assert "s3cretTokenValue" not in delivery.last_error
    logging.getLogger("test").info("posting to %s", WEBHOOK)
    assert "s3cretTokenValue" not in capsys.readouterr().out
    configure_logging("WARNING", [])


def test_delivery_gives_up_after_max_attempts(tmp_path):
    ctx = discord_ctx(tmp_path, [httpx.Response(500)], [])
    trigger_alert(ctx)
    for _ in range(notify.MAX_ATTEMPTS + 2):
        ctx.now.advance(hours=2)
        notify.deliver_pending(ctx)
    [delivery] = deliveries(ctx)
    assert (delivery.status, delivery.attempts) == ("failed", notify.MAX_ATTEMPTS)


def test_test_notification_reports_each_channel(tmp_path):
    ctx = discord_ctx(tmp_path, [httpx.Response(204)], [])
    [result] = notify.send_test(ctx)
    assert (result.channel, result.status, result.event_id) == ("discord", "sent", None)


def test_rule_channel_that_is_not_configured_is_recorded_as_failed(ctx, mock):
    with ctx.sessions() as session:
        product, _ = products.add_product(ctx, session, GPU)
        alerts.create_rule(session, product, "pct_below_baseline", "25", channels=["discord", "mock"])
        session.commit()
    ctx.now.advance(hours=6)
    mock.set_price(GPU, "500.00")
    check_product(ctx, product.id)
    assert [(d.channel, d.status) for d in deliveries(ctx)] == [("discord", "failed"), ("mock", "sent")]


@pytest.mark.parametrize(
    "url",
    ["http://discord.com/api/webhooks/1/t", "https://example.com/api/webhooks/1/t", "https://discord.com/other", "https://169.254.169.254/api/webhooks/1/t"],
)  # fmt: skip
def test_webhook_url_must_point_at_discord(url):
    with pytest.raises(ValueError):
        Settings(_env_file=None, discord_webhook_url=url)


def test_email_notifier_sends_through_smtp_and_maps_errors(monkeypatch, tmp_path):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            sent.append("starttls")

        def login(self, username, password):
            sent.append(("login", username))

        def send_message(self, message):
            if "fail" in message["Subject"]:
                raise OSError("connection reset")
            sent.append(message)

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    settings = Settings(
        _env_file=None, data_dir=tmp_path, smtp_host="smtp.example.com", smtp_from="a@example.com",
        smtp_to="b@example.com", smtp_username="user", smtp_password="hunter2-password",
    )  # fmt: skip
    notifier = EmailNotifier(settings)
    link = "https://www.amazon.com/dp/B0DEMOGPU1"
    notifier.send(NotificationDelivery(channel="email", subject="Price alert", body="PRICE DROP ALERT", link_url=link))
    assert sent[0] == "starttls" and sent[1] == ("login", "user")
    assert sent[2]["To"] == "b@example.com" and link in sent[2].get_content()
    with pytest.raises(NotifyError) as info:
        notifier.send(NotificationDelivery(channel="email", subject="fail", body="x"))
    assert info.value.retryable


# --- Price history chart in alerts ------------------------------------------------------------


def test_chart_image_is_a_valid_png_for_ordinary_and_edge_case_histories():
    import io
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from PIL import Image

    from app.chart import ACCENT, HEIGHT, WIDTH, render_price_chart

    zone, start = ZoneInfo("America/New_York"), datetime(2026, 11, 1, tzinfo=UTC)
    histories = {
        "falling": [(start + timedelta(hours=12 * i), 80000 - 1500 * i) for i in range(12)],
        "two points": [(start, 1999), (start + timedelta(minutes=5), 1999)],
        "same instant": [(start, 500), (start, 400)],
        "long": [(start + timedelta(hours=i), 10000 + (i * 37) % 900) for i in range(500)],
        "one cent": [(start, 1), (start + timedelta(days=1), 2)],
        "yen": [(start, 150000), (start + timedelta(days=3), 128000)],
    }
    for name, points in histories.items():
        currency = "JPY" if name == "yen" else "USD"
        png = render_price_chart(
            points, currency, zone, baseline_minor=points[0][1], target_minor=points[0][1] * 3 // 4
        )
        assert png.startswith(PNG_SIGNATURE), name
        image = Image.open(io.BytesIO(png))
        assert image.size == (WIDTH, HEIGHT), name
        colours = {colour for _count, colour in image.getcolors(maxcolors=WIDTH * HEIGHT)}
        assert ACCENT in colours, name  # the price line was drawn
        assert len(png) < 200_000, name
    assert render_price_chart(histories["falling"], "USD", zone).startswith(PNG_SIGNATURE)  # no reference lines
    with pytest.raises(ValueError):
        render_price_chart(histories["falling"][:1], "USD", zone)


def test_chart_can_be_switched_off(tmp_path):
    seen: list[httpx.Request] = []
    ctx = discord_ctx(tmp_path, [httpx.Response(200, json={"id": "1"})], seen)
    with ctx.sessions() as session:
        product, _ = products.add_product(ctx, session, GPU)
        alerts.create_rule(session, product, "fixed_target", "900")  # already below at the first price
        session.commit()
    check_product(ctx, product.id)  # second observation: the rule added above fires, with a chart
    assert multipart_parts(seen[0])[1] is not None
    ctx.settings.alert_chart_enabled = False
    notify.send_test(ctx)
    ctx.providers["mock"].set_price(GPU, "950.00")
    check_product(ctx, product.id)
    ctx.now.advance(hours=2)
    ctx.providers["mock"].set_price(GPU, "850.00")
    check_product(ctx, product.id)
    assert [multipart_parts(request)[1] is None for request in seen[1:]] == [True, True]  # test message, then an alert
    assert all(request.headers["content-type"].startswith("application/json") for request in seen[1:])


def test_alert_is_sent_without_a_chart_when_only_one_price_exists(tmp_path):
    seen: list[httpx.Request] = []
    ctx = discord_ctx(tmp_path, [httpx.Response(200, json={"id": "1"})], seen)
    ctx.providers["mock"].set_mode("outage")
    with ctx.sessions() as session:
        product, _ = products.add_product(ctx, session, GPU)  # saved without a price
        alerts.create_rule(session, product, "fixed_target", "900")
        session.commit()
    ctx.providers["mock"].set_mode("ok")
    assert check_product(ctx, product.id).status == "ok"  # the very first price is already below the target
    [request] = seen
    payload, chart = multipart_parts(request)
    assert chart is None and "image" not in payload["embeds"][0] and "attachments" not in payload
    assert [d.status for d in deliveries(ctx)] == ["sent"]


def test_rejected_upload_falls_back_to_the_plain_alert(tmp_path):
    seen: list[httpx.Request] = []
    ctx = discord_ctx(
        tmp_path,
        [httpx.Response(400, json={"message": "Invalid Form Body"}), httpx.Response(200, json={"id": "1"})],
        seen,
    )
    trigger_alert(ctx)
    assert len(seen) == 2
    first_upload = multipart_parts(seen[0])[1]
    assert first_upload is not None and first_upload.startswith(PNG_SIGNATURE) and multipart_parts(seen[1])[1] is None
    [delivery] = deliveries(ctx)
    assert (delivery.status, delivery.attempts) == ("sent", 1)  # one delivery attempt, two requests


def test_chart_failure_never_blocks_the_alert(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("font missing")

    monkeypatch.setattr("app.notify.render_price_chart", boom)
    seen: list[httpx.Request] = []
    ctx = discord_ctx(tmp_path, [httpx.Response(200, json={"id": "1"})], seen)
    trigger_alert(ctx)
    assert multipart_parts(seen[0])[1] is None
    assert [d.status for d in deliveries(ctx)] == ["sent"]


def test_chart_is_rendered_once_per_alert_and_shared_by_all_channels(ctx, mock, monkeypatch):
    import app.notify as notify_module

    calls = []
    real = notify_module.render_price_chart

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.notify.render_price_chart", counting)
    second = notify_module.MockNotifier()
    second.name = "email"
    ctx.notifiers["email"] = second
    trigger_alert(ctx)
    assert len(calls) == 1
    assert ctx.notifiers["mock"].charts[0] is not None and ctx.notifiers["mock"].charts == second.charts


def test_email_alert_carries_the_chart_as_an_attachment(monkeypatch, tmp_path):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        smtp_host="smtp.example.com",
        smtp_from="a@example.com",
        smtp_to="b@example.com",
    )
    delivery = NotificationDelivery(channel="email", subject="Price alert", body="PRICE DROP ALERT", link_url=None)
    EmailNotifier(settings).send(delivery, PNG_SIGNATURE + b"fake")
    [attachment] = list(sent[0].iter_attachments())
    assert (attachment.get_content_type(), attachment.get_filename()) == ("image/png", "price-history.png")
    EmailNotifier(settings).send(delivery)
    assert list(sent[1].iter_attachments()) == []


# --- Regression tests for the code review of the chart feature -------------------------------


def test_retry_after_a_rejected_upload_goes_out_without_the_chart(tmp_path):
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(413),
        httpx.Response(429, json={"retry_after": 30}),
        httpx.Response(200, json={"id": "1"}),
    ]
    ctx = discord_ctx(tmp_path, responses, seen)
    trigger_alert(ctx)
    [delivery] = deliveries(ctx)
    assert (delivery.status, delivery.attempts) == ("retrying", 1) and len(seen) == 2
    uploaded = multipart_parts(seen[0])[1]
    assert uploaded is not None and uploaded.startswith(PNG_SIGNATURE) and len(uploaded) > 5000
    assert multipart_parts(seen[1])[1] is None
    ctx.now.advance(minutes=2)
    notify.deliver_pending(ctx)
    assert len(seen) == 3 and multipart_parts(seen[2])[1] is None  # one plain request, no second upload
    assert [(d.status, d.attempts) for d in deliveries(ctx)] == [("sent", 2)]


def test_failed_upload_never_leaks_the_webhook_token(tmp_path):
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    settings = Settings(
        _env_file=None, data_dir=tmp_path, scheduler_enabled=False, log_level="WARNING",
        discord_webhook_url=WEBHOOK, notify_mock_enabled=False,
    )  # fmt: skip
    ctx = make_ctx(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    trigger_alert(ctx)
    [delivery] = deliveries(ctx)
    assert (
        delivery.status == "retrying"
        and "s3cretTokenValue" not in delivery.last_error
        and WEBHOOK not in delivery.last_error
    )


def test_chart_uses_the_newest_prices_of_the_window_in_the_alert_currency(ctx, mock):
    from app.models import AlertEvent, PriceObservation, Product

    captured = {}

    def capture(points, currency, zone, baseline, target):
        captured.update(points=list(points), currency=currency, baseline=baseline, target=target)
        return PNG_SIGNATURE

    product = trigger_alert(ctx)
    with ctx.sessions() as session:
        stored = session.get(Product, product.id)

        def observation(price: int, currency: str = "USD", age_days: int = 0) -> PriceObservation:
            return PriceObservation(
                product_id=stored.id, price_minor=price, item_price_minor=price, currency=currency, provider="mock",
                price_basis="delivered", observed_at=ctx.now() - timedelta(days=age_days),
            )  # fmt: skip

        session.add(observation(99900, age_days=45))
        session.add(observation(70000, currency="EUR"))
        session.add_all(observation(60000 + index) for index in range(notify.CHART_MAX_POINTS + 20))
        session.commit()
        event_id = session.scalars(select(AlertEvent.id)).one()
        import app.notify as notify_module

        original, notify_module.render_price_chart = notify_module.render_price_chart, capture
        try:
            assert notify.chart_for_event(ctx, session, event_id) == PNG_SIGNATURE
            assert notify.chart_for_event(ctx, session, 999999) is None  # a deleted alert
        finally:
            notify_module.render_price_chart = original
    prices = [price for _moment, price in captured["points"]]
    assert len(prices) == notify.CHART_MAX_POINTS
    assert 99900 not in prices and 70000 not in prices  # older than the window, and the other currency
    assert prices[-1] == 60000 + notify.CHART_MAX_POINTS + 19 and prices == sorted(prices)  # newest kept, oldest first
    assert (captured["currency"], captured["baseline"], captured["target"]) == ("USD", 80000, 60000)


def test_chart_labels_fit_and_ticks_are_unique():
    import io
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from PIL import Image

    from app.chart import BACKGROUND, _ticks, render_price_chart

    assert _ticks(0, 2) == [0, 1, 2]
    assert len(_ticks(0.0, 1e15)) <= 6 and len(_ticks(59000.0, 81000.0)) <= 6
    start = datetime(2026, 11, 1, tzinfo=UTC)
    huge = [(start, 1_000_000_000), (start + timedelta(days=5), 990_000_000)]
    image = Image.open(io.BytesIO(render_price_chart(huge, "USD", ZoneInfo("America/New_York"))))
    left_edge = {image.getpixel((0, y)) for y in range(image.height)} | {
        image.getpixel((1, y)) for y in range(image.height)
    }
    assert left_edge == {BACKGROUND}  # no label is cut off at the left border


def test_mock_notifier_memory_is_bounded():
    mock_notifier = notify.MockNotifier()
    for index in range(mock_notifier.KEEP + 50):
        mock_notifier.send(NotificationDelivery(channel="mock", subject="s", body=f"message {index}"), b"x")
    assert len(mock_notifier.sent) == len(mock_notifier.charts) == mock_notifier.KEEP
    assert mock_notifier.sent[-1] == f"message {mock_notifier.KEEP + 49}"
