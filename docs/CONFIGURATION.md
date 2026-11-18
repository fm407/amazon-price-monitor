# Configuration

All configuration comes from environment variables. With Docker Compose, put them in `.env`.
After a change, run `docker compose up -d` to recreate the container.

A value left empty (`KEY=`) means "not set".

## Application

| Variable | Default | Meaning |
|---|---|---|
| `TIMEZONE` | `America/New_York` | Time zone for displayed dates. Storage is always UTC. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `BIND_ADDRESS` | `127.0.0.1` | Host address the dashboard is published on (Compose only). |
| `HOST_PORT` | `8080` | Host port (Compose only). |
| `SECRET_KEY` | generated | Session signing key, 32 characters or more. Empty: generated once and stored in the data volume. |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` when the dashboard is served over HTTPS. |
| `SESSION_MAX_AGE_HOURS` | `168` | How long a login lasts. |
| `API_TOKEN` | empty | Optional bearer token for REST clients, 32 characters or more. Empty disables token access. |
| `DATA_DIR` | `/app/data` in the container | Location of the database and generated secrets. |
| `DATABASE_URL` | SQLite file in `DATA_DIR` | Advanced. SQLAlchemy URL. |

Generate a token with `openssl rand -hex 32`.

## Price providers

| Variable | Default | Meaning |
|---|---|---|
| `PROVIDER` | `mock` | `mock`, `canopy` or `apify`. |
| `FALLBACK_PROVIDER` | empty | `canopy` or `apify`. Tried when the primary provider fails. Must differ from `PROVIDER`. Not allowed with `PROVIDER=mock`. |
| `DEMO_MODE` | `false` (`true` in `.env.example`) | Seeds three fictional demo products. Requires `PROVIDER=mock`. `false` removes them at start. |
| `HTTP_TIMEOUT_SECONDS` | `20` | Timeout for outbound HTTP requests. |
| `PROVIDER_MAX_ATTEMPTS` | `3` | Attempts per provider and check. Every attempt counts against the budget. |
| `CANOPY_API_KEY` | empty | Canopy API key. Secret. |
| `CANOPY_COST_PER_REQUEST_USD` | `0.01` | Your estimate of the cost of one Canopy request. |
| `CANOPY_MAX_CONCURRENCY` | `2` | Parallel Canopy requests. |
| `CANOPY_BASE_URL` | `https://rest.canopyapi.co` | Advanced. |
| `APIFY_TOKEN` | empty | Apify API token. Secret. |
| `APIFY_ACTOR_ID` | empty | Actor to run, as `username/actor-name` or an Actor ID. There is no default. |
| `APIFY_INPUT_TEMPLATE` | see `.env.example` | JSON input for the Actor. Placeholders: `{url}`, `{asin}`, `{marketplace}`, `{domain}`. |
| `APIFY_FIELD_MAP` | empty | JSON object that overrides the output field mapping. |
| `APIFY_COST_PER_RUN_USD` | `0.01` | Your estimate of the cost of one Actor run. |
| `APIFY_MAX_CHARGE_PER_RUN_USD` | empty | Optional cost cap per run, enforced by Apify. |
| `APIFY_RUN_TIMEOUT_SECONDS` | `180` | Run time limit sent to Apify. |
| `APIFY_MAX_CONCURRENCY` | `1` | Parallel Actor runs. |
| `APIFY_BASE_URL` | `https://api.apify.com` | Advanced. |

## Cost controls

| Variable | Default | Meaning |
|---|---|---|
| `ALLOW_PAID_API_REQUESTS` | `false` | Paid providers are never called while this is `false`. |
| `MAX_MONTHLY_API_SPEND_USD` | `5.00` | Hard limit per calendar month (UTC) across all paid providers. |
| `MAX_DAILY_API_REQUESTS` | `100` | Hard limit on paid requests per UTC day. |
| `DEFAULT_CHECK_INTERVAL_MINUTES` | `360` | Check interval for products without their own interval. Minimum 5. |

## Scheduler

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | `false` disables automatic checks. Manual checks still work. |
| `SCHEDULER_TICK_SECONDS` | `30` | How often the scheduler looks for due products. |
| `SCHEDULER_BATCH_SIZE` | `5` | Maximum products checked per tick. |

## Notifications

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | empty | Discord webhook URL. Secret: it contains the webhook token. |
| `DISCORD_ENABLED` | `true` | `false` turns Discord off without removing the URL. |
| `SMTP_HOST` | empty | SMTP server. Email is active when host, sender and recipient are set. |
| `SMTP_PORT` | `587` | SMTP port. |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none`. |
| `SMTP_USERNAME` | empty | SMTP user. |
| `SMTP_PASSWORD` | empty | SMTP password. Secret. |
| `SMTP_FROM` | empty | Sender address. |
| `SMTP_TO` | empty | Recipient address. |
| `NOTIFY_MOCK_ENABLED` | on in mock mode | Records notifications in the dashboard instead of sending them. |

## Secrets from files

For production you can keep secrets out of `.env`. The application reads any setting from a file in
`/run/secrets` whose name is the variable name in lower case.

```yaml
# compose.override.yaml
services:
  app:
    secrets: [canopy_api_key, discord_webhook_url]
secrets:
  canopy_api_key:
    file: ./secrets/canopy_api_key
  discord_webhook_url:
    file: ./secrets/discord_webhook_url
```

```bash
mkdir -p secrets && chmod 700 secrets
printf '%s' 'your-key' > secrets/canopy_api_key
chmod 600 secrets/*
docker compose up -d
```

Environment variables take precedence over secret files. The `secrets/` folder is ignored by Git.

## Settings stored in the database

The `application_settings` table holds only non-secret state: the administrator name, the password
hash and the month for which a budget notice was sent. API keys are never stored in the database.
