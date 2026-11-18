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
    payload = json.loads(request.content)
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
