# Notifications

Alerts are sent to a Discord channel through a webhook. You do not need a bot.
Email through SMTP is optional.

## Discord

### 1. Create the webhook

1. In Discord, open the server where you want the alerts.
2. Open **Server Settings > Integrations > Webhooks**.
3. Press **New Webhook**. Choose a name and the channel.
4. Press **Copy Webhook URL**.

The URL looks like `https://discord.com/api/webhooks/123456789012345678/AbCdEf...`.
The last part is a secret token. Anyone who has the URL can post to your channel.

For a private setup, create your own server with one channel and mute everything except that channel.

### 2. Configure the application

```ini
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/your-token
```

```bash
docker compose up -d
```

The application accepts only `https://discord.com/api/webhooks/...` URLs and refuses to start with
anything else.

### 3. Test

Open **Settings** in the dashboard and press **Send test notification**.
The result appears below the button for each channel.

### What an alert looks like

```
PRICE DROP ALERT

Product: ASRock Intel Arc Pro B60 24GB
Previous reference price: $800.00
Current price: $579.99
Discount: 27.50%
Target price: $600.00
Savings: $220.01
Shipping: unknown (not included in the price)
Seller: Amazon.com
Condition: new
Availability: In stock
Provider: Canopy
Rule: 25% below baseline
Checked: November 18, 2026, 12:30 PM EST
```

The embed title links to the Amazon product page. The product image is shown as a thumbnail when the
provider supplies one. Values the provider did not supply are shown as `unknown`.
Product titles are escaped, and mentions such as `@everyone` are disabled.

### Price history chart

Each alert includes a chart of the product's price history, drawn by the application itself:
the stored prices of the last 30 days, the baseline as a grey dashed line, the target as a green
dashed line and the latest price in the top right corner. In Discord the chart appears inside the
alert. In email it is an attachment.

| Setting | Default | Meaning |
|---|---|---|
| `ALERT_CHART_ENABLED` | `true` | Set `false` to send alerts without a chart. |
| `ALERT_CHART_DAYS` | `30` | Days of history shown. At most the latest 500 prices are drawn. |

- A chart needs at least two stored prices. The first alert of a brand-new product may come without one.
- If the chart cannot be drawn, or Discord does not accept the upload, the alert is sent without it.
  An alert is never held back because of the chart.
- The test notification has no chart.
- The chart shows prices and dates only. Product titles are not drawn into the image.

### Delivery and retries

- An alert and its deliveries are saved in the database before anything is sent.
- Temporary failures (rate limits, server errors, network errors) are retried up to 5 times with
  growing delays. A `retry_after` value from Discord is honoured.
- Permanent failures, such as a deleted webhook, are marked `failed` and not retried.
- Every delivery and its status is listed under **Activity**.

## Email (optional)

```ini
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=you@example.com
SMTP_PASSWORD=app-password
SMTP_FROM=you@example.com
SMTP_TO=you@example.com
```

Email is active when `SMTP_HOST`, `SMTP_FROM` and `SMTP_TO` are set.
Use an app password if your mail provider offers one.

## Choosing channels per alert rule

By default a rule uses every configured channel. To limit a rule, open the product, tick the channels
under the rule and press **Save**.

## Adding another channel later

A channel is a class with a `name` and one method, `send(delivery)`, registered in
`build_notifiers` in `app/notify.py`. Raise `NotifyError` with `retryable=True` for temporary failures.
