"""Notification channels and delivery tracking.

Alert events and their deliveries are persisted before any message is sent.
Delivery then runs under one lock, so a message is never sent twice.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_config import redact
from app.models import AlertEvent, AlertRule, NotificationDelivery, PriceObservation, Product
from app.money import format_money, percent_of
from app.providers import provider_label

if TYPE_CHECKING:
    from app.context import AppContext

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 60.0
DISCORD_TITLE_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 4096
DISCORD_COLOR = 0x2ECC71
MARKDOWN_RE = re.compile(r"([\\*_~`|>\[\]()#-])")
HEADLINES = {"trigger": "PRICE DROP ALERT", "further_drop": "PRICE DROPPED FURTHER", "reminder": "PRICE STILL LOW"}
AVAILABILITY = {"in_stock": "In stock", "out_of_stock": "Out of stock"}


class NotifyError(Exception):
    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


class Notifier(Protocol):
    name: str

    def send(self, delivery: NotificationDelivery) -> None: ...


class MockNotifier:
    """Records messages instead of sending them. Used in mock mode, demos and tests."""

    name = "mock"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, delivery: NotificationDelivery) -> None:
        self.sent.append(delivery.body)
        log.info("mock notification delivered", extra={"subject": delivery.subject})


class DiscordNotifier:
    """Posts to a Discord webhook (Execute Webhook endpoint). No bot account is needed."""

    name = "discord"

    def __init__(self, client: httpx.Client, webhook_url: str) -> None:
        self._client = client
        self._url = webhook_url

    @staticmethod
    def escape(text: str) -> str:
        """Escape Discord markdown so product titles cannot change the message formatting."""
        return MARKDOWN_RE.sub(r"\\\1", text)

    @classmethod
    def payload(cls, delivery: NotificationDelivery) -> dict[str, object]:
        headline, _, rest = delivery.body.partition("\n")
        embed: dict[str, object] = {
            "title": headline[:DISCORD_TITLE_LIMIT],
            "description": cls.escape(rest.strip())[:DISCORD_DESCRIPTION_LIMIT],
            "color": DISCORD_COLOR,
        }
        if delivery.link_url:
            embed["url"] = delivery.link_url
        if delivery.image_url and delivery.image_url.startswith("https://"):
            embed["thumbnail"] = {"url": delivery.image_url}
        # allowed_mentions with an empty parse list: text such as @everyone never pings anyone.
        return {"username": "Price Monitor", "embeds": [embed], "allowed_mentions": {"parse": []}}

    def send(self, delivery: NotificationDelivery) -> None:
        try:
            # wait=true makes Discord confirm that the message was stored instead of returning 204 blindly.
            response = self._client.post(self._url, params={"wait": "true"}, json=self.payload(delivery))
        except httpx.TimeoutException as exc:
            raise NotifyError("Discord request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise NotifyError(f"Discord network error: {type(exc).__name__}", retryable=True) from exc
        if response.status_code in (200, 204):
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        detail = str(body.get("message", "")) if isinstance(body, dict) else ""
        message = f"Discord returned HTTP {response.status_code}: {detail}"[:300]
        if response.status_code == 429:
            retry_after = body.get("retry_after") if isinstance(body, dict) else None
            header = response.headers.get("retry-after", "")
            if isinstance(retry_after, int | float):
                seconds: float | None = float(retry_after)
            else:
                seconds = float(header) if header.replace(".", "", 1).isdigit() else None
            raise NotifyError(message, retryable=True, retry_after=seconds)
        raise NotifyError(message, retryable=response.status_code >= 500)


class EmailNotifier:
    name = "email"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send(self, delivery: NotificationDelivery) -> None:
        cfg = self._settings
        message = EmailMessage()
        message["Subject"] = delivery.subject
        message["From"] = cfg.smtp_from
        message["To"] = cfg.smtp_to
        message.set_content(delivery.body + (f"\n\n{delivery.link_url}" if delivery.link_url else ""))
        timeout = cfg.http_timeout_seconds
        try:
            smtp: smtplib.SMTP
            if cfg.smtp_security == "ssl":
                smtp = smtplib.SMTP_SSL(
                    cfg.smtp_host, cfg.smtp_port, timeout=timeout, context=ssl.create_default_context()
                )
            else:
                smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
            with smtp:
                if cfg.smtp_security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if cfg.smtp_username:
                    smtp.login(cfg.smtp_username, cfg.smtp_password)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise NotifyError("SMTP authentication failed", retryable=False) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise NotifyError("SMTP server refused the recipient", retryable=False) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"SMTP error: {type(exc).__name__}", retryable=True) from exc


def build_notifiers(settings: Settings, client: httpx.Client) -> dict[str, Notifier]:
    notifiers: dict[str, Notifier] = {}
    if settings.discord_configured:
        notifiers["discord"] = DiscordNotifier(client, settings.discord_webhook_url)
    if settings.smtp_configured:
        notifiers["email"] = EmailNotifier(settings)
    if settings.mock_notifier_enabled:
        notifiers["mock"] = MockNotifier()
    return notifiers


def format_local(moment: datetime, settings: Settings) -> str:
    local = moment.astimezone(settings.tz)
    return f"{local:%B} {local.day}, {local.year}, {local.hour % 12 or 12}:{local:%M} {local:%p} {local:%Z}"


def alert_text(
    product: Product, event: AlertEvent, observation: PriceObservation, rule_text: str, settings: Settings
) -> tuple[str, str]:
    """Plain-text subject and body. Unknown values are shown as 'unknown', never invented."""
    currency = event.currency
    reference, price = event.reference_minor, event.price_minor
    discount = percent_of(reference - price, reference) if reference and reference > price else None
    savings = format_money(reference - price, currency) if reference and reference > price else "unknown"
    if observation.shipping_minor is None:
        shipping = "unknown (not included in the price)"
    elif observation.shipping_minor == 0:
        shipping = "free"
    else:
        shipping = f"{format_money(observation.shipping_minor, currency)} (included in the price)"
    headline = (
        "BACK IN STOCK" if event.rule_kind == "back_in_stock" else HEADLINES.get(event.event_type, HEADLINES["trigger"])
    )
    lines = [
        headline,
        "",
        f"Product: {product.title}",
        f"Previous reference price: {format_money(reference, currency)}",
        f"Current price: {format_money(price, currency)}",
        f"Discount: {discount}%" if discount is not None else "Discount: unknown",
        f"Target price: {format_money(event.target_minor, currency)}",
        f"Savings: {savings}",
        f"Shipping: {shipping}",
        f"Seller: {observation.seller or 'unknown'}",
        f"Condition: {observation.condition}",
        f"Availability: {AVAILABILITY.get(observation.availability, 'unknown')}",
        f"Provider: {provider_label(observation.provider)}",
        f"Rule: {rule_text}",
        f"Checked: {format_local(observation.observed_at, settings)}",
    ]
    if observation.coupon_text:
        lines.append(f"Coupon shown (not applied to the price): {observation.coupon_text}")
    if product.is_demo or observation.provider == "mock":
        lines.append("NOTE: fictional demonstration price from the mock provider.")
    subject = f"Price alert: {product.title[:120]} now {format_money(price, currency)}"
    return subject, "\n".join(lines)


def queue(
    session: Session,
    ctx: AppContext,
    *,
    subject: str,
    body: str,
    channels: list[str] | None = None,
    event_id: int | None = None,
    image_url: str | None = None,
    link_url: str | None = None,
) -> list[NotificationDelivery]:
    """Persist one pending delivery per channel. An empty channel list means every enabled channel."""
    deliveries = []
    for channel in channels or list(ctx.notifiers):
        delivery = NotificationDelivery(
            event_id=event_id,
            channel=channel,
            subject=subject[:300],
            body=body,
            image_url=image_url,
            link_url=link_url,
            created_at=ctx.now(),
            next_attempt_at=ctx.now(),
        )
        if channel not in ctx.notifiers:
            delivery.status = "failed"
            delivery.last_error = f"The {channel} channel is not configured."
            delivery.next_attempt_at = None
        session.add(delivery)
        deliveries.append(delivery)
    session.flush()
    return deliveries


def queue_alert(
    session: Session,
    ctx: AppContext,
    event: AlertEvent,
    rule: AlertRule,
    product: Product,
    observation: PriceObservation,
) -> None:
    from app.alerts import describe  # local import: alerts does not depend on notify

    subject, body = alert_text(product, event, observation, describe(rule, event.currency), ctx.settings)
    queue(
        session,
        ctx,
        subject=subject,
        body=body,
        channels=list(rule.channels),
        event_id=event.id,
        image_url=product.image_url,
        link_url=product.url,
    )


def _attempt(ctx: AppContext, session: Session, delivery: NotificationDelivery) -> None:
    now = ctx.now()
    delivery.attempts += 1
    try:
        ctx.notifiers[delivery.channel].send(delivery)
    except NotifyError as err:
        delivery.last_error = redact(err.message)[:500]
        if err.retryable and delivery.attempts < MAX_ATTEMPTS:
            wait = max(err.retry_after or 0, RETRY_BASE_SECONDS * 2 ** (delivery.attempts - 1))
            delivery.status, delivery.next_attempt_at = "retrying", now + timedelta(seconds=wait)
        else:
            delivery.status, delivery.next_attempt_at = "failed", None
        log.warning(
            "notification delivery failed",
            extra={"delivery_id": delivery.id, "channel": delivery.channel, "status": delivery.status},
        )
    except Exception as exc:  # a notifier bug must not block other deliveries
        log.exception("notifier raised an unexpected error", extra={"channel": delivery.channel})
        delivery.status, delivery.next_attempt_at = "failed", None
        delivery.last_error = f"Unexpected error: {type(exc).__name__}"
    else:
        delivery.status, delivery.sent_at, delivery.next_attempt_at, delivery.last_error = "sent", now, None, None
        log.info("notification delivered", extra={"delivery_id": delivery.id, "channel": delivery.channel})
    session.commit()


def deliver_pending(ctx: AppContext) -> int:
    """Send every due delivery. Returns the number of attempts made."""
    with ctx.delivery_lock, ctx.sessions() as session:
        due = session.scalars(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.status.in_(("pending", "retrying")),
                NotificationDelivery.next_attempt_at <= ctx.now(),
            )
            .order_by(NotificationDelivery.id)
        ).all()
        for delivery in due:
            if delivery.channel in ctx.notifiers:
                _attempt(ctx, session, delivery)
        return len(due)


def send_test(ctx: AppContext) -> list[NotificationDelivery]:
    """Queue and send a test message on every enabled channel."""
    body = (
        "TEST NOTIFICATION\n\nAmazon Price Monitor can reach this channel.\n"
        + f"Sent: {format_local(ctx.now(), ctx.settings)}"
    )
    with ctx.sessions() as session:
        ids = [d.id for d in queue(session, ctx, subject="Amazon Price Monitor test notification", body=body)]
        session.commit()
    deliver_pending(ctx)
    with ctx.sessions() as session:
        return list(session.scalars(select(NotificationDelivery).where(NotificationDelivery.id.in_(ids))))
