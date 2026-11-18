# Troubleshooting

Start with the log and the health endpoint:

```bash
docker compose ps
docker compose logs --tail 100 app
curl -s http://127.0.0.1:8080/api/v1/health
```

Each log line is one JSON object. `job_id` ties together all lines of one request or one price check.
The **Activity** page lists failed and skipped provider requests with their reason.

## Starting and signing in

| Problem | Cause and fix |
|---|---|
| The container restarts in a loop | A setting is invalid. The last log lines name it, for example `DEMO_MODE=true requires PROVIDER=mock` or `API_TOKEN must be at least 32 characters`. Fix `.env`, then `docker compose up -d`. |
| `port is already allocated` | Another program uses port 8080. Set `HOST_PORT=8081` in `.env`. |
| I cannot find the setup token | `docker compose logs app \| grep setup_token`. If the log was rotated: `docker compose exec app cat /app/data/setup-token`. |
| "The setup token is not correct" | Copy the token without quotes or spaces. After 5 wrong attempts, wait 15 minutes. |
| I forgot the password | `docker compose exec app python -m app reset-admin`, then `docker compose restart app`, then open `/setup` with the new token. Products and history are kept. |
| "Too many failed attempts" | Wait 15 minutes, or restart the container to clear the counter. |
| The page is not reachable from another computer | Expected. The dashboard listens on `127.0.0.1`. Use an SSH tunnel or see "LAN access" in [DOCKER.md](DOCKER.md). |
| I am signed out after every restart | The data volume is not persistent, so a new session key is generated each time. Check the `volumes:` section of `compose.yaml`. |
| "Missing or invalid CSRF token" | The page was open across a login or restart. Reload it and try again. |
| `data directory is not writable` in the health check | With a bind mount, the folder must belong to uid 10001: `sudo chown 10001:10001 data`. |

## Prices

| Problem | Cause and fix |
|---|---|
| A product shows "no valid price yet" | The first check did not return a usable price. Open the product: the yellow note gives the reason. |
| "Paid API requests are disabled" | Set `ALLOW_PAID_API_REQUESTS=true`. |
| "The monthly API budget is exhausted" | Raise `MAX_MONTHLY_API_SPEND_USD` or wait for the next month. The application keeps running. |
| "The daily API request limit is reached" | Raise `MAX_DAILY_API_REQUESTS`, check less often, or wait for the next UTC day. |
| "The provider is temporarily paused" | The provider answered with an authentication, quota or rate-limit error. It is retried automatically after the pause (one hour for authentication and quota errors). Check the API key and your plan. A restart clears the pause. |
| `auth` errors under Activity | The API key or token is wrong or expired. |
| "The product is out of stock" | No price is stored while a product is out of stock. The last valid price stays. |
| "The current offer is used or refurbished" | The Buy Box currently belongs to a used offer. Enable "Include used or refurbished offers" for the product if you want those prices. |
| "The current offer is not sold by Amazon" | The product is set to "Amazon as seller only" and another seller holds the offer, or the provider did not name the seller. |
| "Price is in EUR; this product is tracked in USD" | The provider returned a different currency. Prices in different currencies are never compared. |
| "The provider returned no valid currency" (Apify) | Map `currency` to the right output field in `APIFY_FIELD_MAP`. |
| Apify: "Actor, run or dataset not found" | `APIFY_ACTOR_ID` is wrong. Use `username/actor-name`. |
| Apify: "returned no product for this ASIN" | The Actor ran but produced no item. Run it once in the Apify console with the same input and compare its input schema with `APIFY_INPUT_TEMPLATE`. |
| A product was paused by itself | The provider reported "not found" three times in a row. Check the link, then press **Resume monitoring**. |
| "This is not a recognised Amazon product link" | Use a product page link from `amazon.com` (it contains `/dp/` and a 10-character ASIN) or the ASIN itself. Search and category links do not work. |
| Other Amazon sites are rejected | Only `amazon.com` is enabled in this release. |
| The price includes or excludes shipping unexpectedly | The observation table shows the shipping column. "unknown" means the provider gave no shipping cost and the monitored price is the item price only. |
| Checks run later than expected | Each check has a few minutes of random delay, and at most `SCHEDULER_BATCH_SIZE` products are checked per tick. After a restart, overdue checks are spread over 10 minutes. |

## Alerts and notifications

| Problem | Cause and fix |
|---|---|
| No alert although the price is below the target | Alerts fire when the price *crosses* the target. If the rule was created while the price was already below, or it already alerted, it stays quiet until the price rises above the target and drops again. Turn on reminders for repeated messages. |
| No second alert shortly after the first | The rule's cooldown (60 minutes by default) is still running. The alert is sent at the next check after the cooldown. |
| "Percent below historical average" never fires | It needs five earlier observations inside its lookback window. |
| "Send test notification" fails for Discord with HTTP 404 | The webhook was deleted or the URL is incomplete. Create a new webhook. |
| Discord test fails with a network error | The server cannot reach `discord.com`. Check outbound HTTPS, DNS and any network filter. |
| Discord alerts arrive late | Discord rate-limited the webhook. Deliveries are retried automatically. See their status under Activity. |
| A notification shows `failed` | Open Activity for the error text. Permanent errors are not retried. Fix the cause and wait for the next alert. |
| Email fails with "SMTP authentication failed" | Wrong user or password. Many providers need an app password. |
| "No notification channel is configured" | Set `DISCORD_WEBHOOK_URL` or the SMTP settings. |

## Database

| Problem | Cause and fix |
|---|---|
| "The database is busy or unavailable" | Another process holds the database, for example a manual `sqlite3` session. Close it. The application waits up to 10 seconds per write. |
| Restore after a mistake | See "Restore" in [DOCKER.md](DOCKER.md). |
| Inspect the data | `docker compose exec app python -c "import sqlite3; c=sqlite3.connect('/app/data/monitor.db'); print(c.execute('select id, asin, title from products').fetchall())"` |
| Start from scratch | `docker compose down -v` deletes everything, including the account. |

## Demo data

| Problem | Cause and fix |
|---|---|
| Demo products still appear | Set `DEMO_MODE=false` and run `docker compose up -d`. They are removed at start. |
| No demo products appear | `DEMO_MODE=true` needs `PROVIDER=mock`. |
| Mock prices returned to their defaults | Simulated failure modes are kept in memory and reset at restart. Simulated prices continue from the last stored price. |

## More detail

Set `LOG_LEVEL=DEBUG` and restart. Each alert rule evaluation is then logged with its target and result.
