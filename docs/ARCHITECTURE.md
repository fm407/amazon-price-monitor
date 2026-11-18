# Architecture

## Overview

One Python process serves the dashboard and the REST API and runs the scheduler.
All state is in one SQLite database on a Docker volume.

```
                        +--------------------- one process, one worker ---------------------+
 Browser  -- HTTP -->   |  FastAPI                                                          |
 REST client            |    web.py   server-rendered pages (Jinja2, HTMX, Chart.js)         |
                        |    api.py   REST API under /api/v1                                 |
                        |                 |                                                  |
                        |    products.py  alerts.py  budget.py  notify.py   (services)       |
                        |                 |                                                  |
                        |    scheduler.py --> checker.py  (the check pipeline)               |
                        |                       |-- budget.py      reserve cost, hard limits |
                        |                       |-- providers/     Canopy | Apify | Mock     |
                        |                       |-- normalize.py   is this a valid price?    |
                        |                       |-- alerts.py      rule evaluation           |
                        |                       '-- notify.py      Discord | SMTP | Mock     |
                        +----------------------------------|--------------------------------+
                                                           v
                                     SQLite (WAL) in the volume at /app/data
```

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Settings from environment variables and secret files. Validation at startup. |
| `context.py` | `AppContext`: settings, database sessions, HTTP client, providers, notifiers, clock, locks. |
| `db.py` | Engine, SQLite pragmas, UTC datetime type, migration runner. |
| `models.py` | Tables. |
| `amazon.py` | ASIN extraction, URL normalization, safe short-link resolution. |
| `money.py` | Decimal parsing, minor units, formatting, discount maths. |
| `providers/` | One adapter per provider behind `PriceProvider`. |
| `normalize.py` | Decides whether a provider response contains a valid, eligible price. |
| `budget.py` | Request ledger, spending guard, usage statistics. |
| `checker.py` | The check pipeline: fetch, retry, fallback, record, schedule. |
| `alerts.py` | Threshold maths, alert state machine, event creation. |
| `notify.py` | Channels, message text, delivery tracking and retries. |
| `scheduler.py` | Background thread that runs due checks. |
| `products.py` | Product service: add, edit, baseline, history, export. |
| `security.py` | Password hashing, sessions, CSRF, throttling, setup token, headers. |
| `api.py`, `schemas.py` | REST routes and their request and response models. |
| `web.py`, `templates/`, `static/` | Dashboard. |
| `demo.py` | Demo data for mock mode. |
| `main.py` | Application factory, middleware, error handlers, lifecycle. |

Dependencies point one way: routes call services, services call the pipeline, the pipeline calls
providers and notifiers. `AppContext` is passed explicitly, which is all the dependency injection the
application needs. Tests build a context with a fake clock, a no-op sleep and mocked HTTP.

## The check pipeline

`check_product(ctx, product_id)` is the only way a price enters the system. The scheduler, the
"Check price now" button, the REST endpoint and the add-product flow all call it.

1. **Lock.** An in-process set holds the IDs of products being checked. A second check of the same
   product returns "busy". Checks never overlap.
2. **Read** the product, then close the database session. No session is held during network calls.
3. **Fetch** through the provider chain (primary, then fallback). Each attempt:
   - is skipped when the provider is paused after an authentication, quota or rate-limit error;
   - reserves its cost in the ledger (see below) or is denied;
   - runs inside a per-provider concurrency limit;
   - is retried only for temporary errors, with exponential backoff, jitter and `Retry-After`.
4. **Re-read** the product. If it was deleted meanwhile, stop.
5. **Normalize.** Reject out-of-stock, missing, zero, negative, non-numeric, wrong-currency,
   used-condition and filtered-seller results. A rejected result changes nothing except the
   "last check" message.
6. **Record** the observation and update latest, previous, lowest and highest price. The first valid
   price sets the baseline.
7. **Evaluate** every enabled alert rule. Save alert events and their pending deliveries.
8. **Commit** observation, product state, events and deliveries in one transaction.
9. **Deliver** pending notifications.
10. **Schedule** the next check and release the lock.

Any unexpected exception is caught, logged with the job ID and reported as a failed check. One
failing product cannot stop the scheduler.

## Scheduling

Each product stores `next_check_at`. The scheduler thread wakes every `SCHEDULER_TICK_SECONDS`,
selects up to `SCHEDULER_BATCH_SIZE` active products that are due, checks them in a small thread
pool, waits for the batch, then retries due notifications.

| Requirement | How it is met |
|---|---|
| Per-product interval | `check_interval_minutes` on the product, else the default. |
| Jitter | Up to 10% of the interval, at most 10 minutes, added to each next check. |
| No overlap | The in-process lock described above. |
| Backoff | After a temporary failure the next check comes sooner: 15, 30, 60 minutes, capped at the interval. |
| No endless retries | Authentication and quota errors pause the provider for an hour. Three "not found" results in a row pause the product. |
| Persistence | The schedule is data, not in-memory jobs. Nothing needs to be re-registered after a restart. |
| Restart without a burst | At start, overdue checks are spread randomly over 10 minutes, and each tick is limited to one batch. |
| Recovery | Requests that were in flight during a crash are marked `interrupted`. Their reserved cost still counts. |

### Why a single scheduler owner is safe here

The deployment runs exactly one process with one worker (`python -m app` starts it that way). The
scheduler is a thread in that process, so there is one owner by construction. The per-product lock,
the provider concurrency limits, the budget lock and the delivery lock are in-process objects, and
they are correct because no second process exists.

A custom loop was chosen over APScheduler because the schedule must be persistent and
restart-safe anyway. With `next_check_at` in the database, a polling loop of about 60 lines covers
every requirement, and there is no second job store to keep in sync with the products table.

To scale beyond one process, these locks would have to move into the database (for example
`SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL). The code keeps them behind small functions
(`try_begin_check`, `budget.reserve`, `deliver_pending`) so that this change stays local.

## Money and price accuracy

- Prices are parsed into `Decimal` and stored as integer minor units (cents for USD).
- API cost is stored as integer micro-dollars. No floating point is used for stored money.
- Each observation stores item price, shipping (or unknown), delivered price (when shipping is
  known), list price, seller, condition, availability, provider and a trimmed copy of the raw fields.
- The **monitored price** is the delivered price when shipping is known, otherwise the item price.
  The observation records which one it is (`price_basis`: `delivered` or `item_only`).
- A product has one currency, set by its first valid price. A later price in another currency is
  rejected.
- List prices and coupons are stored for information and never used for alerts.

## Alert engine

Rule kinds: `pct_below_baseline`, `fixed_target`, `pct_below_average`, `new_low`,
`pct_drop_since_last`.

Threshold rules (the first three) keep an `is_below` flag:

| Previous state | Price now | Result |
|---|---|---|
| above | at or below target | **Alert** (unless in cooldown; then the rule stays armed and alerts later) |
| below | still at or below | Nothing, or a reminder, or a further-drop alert if configured |
| below | above target | Flag cleared. The rule can alert again. |

Event rules (`new_low`, `pct_drop_since_last`) alert on each occurrence, limited by the cooldown.
The average rule stays inactive until five earlier observations exist in its lookback window.

Duplicate protection has three layers: the state flag, a unique database constraint on
(rule, observation), and a unique constraint on (event, channel) for deliveries. Deliveries are sent
under one lock, and each is marked sent in the database before the next one starts.

## Data model

| Table | Purpose | On product delete |
|---|---|---|
| `products` | Identity, monitoring settings, price state, schedule. Unique on (ASIN, marketplace). | |
| `price_observations` | Every valid observed price. | deleted |
| `alert_rules` | Rules and their state. | deleted |
| `alert_events` | Each alert that fired. | deleted (kept when only the rule is deleted) |
| `notification_deliveries` | One row per event and channel, with attempts and status. | deleted with the event |
| `provider_requests` | Ledger of every provider request and skip, with cost. | kept, product reference cleared |
| `application_settings` | Non-secret key/value state. | |

There is no `scheduler_state` table. The schedule is the `next_check_at` column, which is all the
persistent scheduler state this design needs.

Schema changes go through Alembic migrations in `migrations/`. They run at startup. A test compares
the migrated schema with the models, so they cannot drift apart.

### PostgreSQL later

The code uses SQLAlchemy 2 with portable types and no SQLite-specific SQL. `DATABASE_URL` already
selects the engine. Moving to PostgreSQL needs a driver, the lock changes described above, and a test
run against PostgreSQL. It was left out because one SQLite file meets the requirement.

## Extension points

| To add | Where |
|---|---|
| A marketplace | `MARKETPLACES` and `SUPPORTED_MARKETPLACES` in `amazon.py`. Products already store marketplace and currency. |
| A provider | A class with `fetch_product` in `providers/`, registered in `build_providers`. |
| A notification channel | A class with `send` in `notify.py`, registered in `build_notifiers`. |
| An alert type | A branch in `alerts.evaluate_condition` and a label in `KIND_LABELS`. |
| Another retailer | A new identity parser next to `amazon.py` and a provider. `products.marketplace` would become a retailer plus region. |

## Request tracing

Every HTTP request gets an ID (`req-...`), returned in `X-Request-ID`. Every check gets a job ID
(`check-<product>-<random>`). Each log line is one JSON object and carries that ID in `job_id`.
