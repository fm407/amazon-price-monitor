# Price providers

The application never scrapes Amazon itself. It asks a provider for product data.
Every provider implements one interface: `fetch_product(asin, marketplace)`.

| Provider | Account needed | Cost | Use |
|---|---|---|---|
| `mock` | no | free | Demos, development, tests. Fictional prices. |
| `canopy` | yes | per request | Recommended. One HTTP request per check. |
| `apify` | yes | per Actor run | Alternative or fallback. One Actor run per check. |

Each stored price records which provider supplied it.

## Mock mode (no credentials)

```ini
PROVIDER=mock
DEMO_MODE=true
```

- Any ASIN works and returns a predictable fictional price.
- With `DEMO_MODE=true`, three labelled demo products are created with 12 fictional price points each.
- On each product page, **Simulate a mock price** sets a price or a failure (no price, out of stock,
  outage, rate limit, invalid response) and runs a check. Use it to try alerts.
- A banner on every page states that the prices are fictional.
- The mock provider is only available when `PROVIDER=mock`. Fictional and real prices never mix.

Set `DEMO_MODE=false` and restart to remove the demo products.

## Canopy

Verified against the OpenAPI document published at `https://rest.canopyapi.co/api/v1/openapi.json`.

| Item | Value |
|---|---|
| Request | `GET https://rest.canopyapi.co/api/amazon/product?asin=<ASIN>&domain=US` |
| Authentication | Header `API-KEY: <key>` |
| Documented responses | `200`, `400`, `401`, `402`, `500` |

Setup:

1. Create an account at <https://www.canopyapi.co> and copy your API key from the dashboard.
2. Check the price per request of your plan.
3. Set in `.env`:
   ```ini
   PROVIDER=canopy
   DEMO_MODE=false
   CANOPY_API_KEY=your-key
   CANOPY_COST_PER_REQUEST_USD=0.01
   ALLOW_PAID_API_REQUESTS=true
   ```
4. Run `docker compose up -d`.

How the response is used:

| Canopy field | Meaning here |
|---|---|
| `price.value`, `price.currency` | Current item price of the Buy Box offer. This is the monitored price. |
| `recommendedRetailPrice` | List price. Stored for reference, never used as the monitored price. |
| `isInStock` | Availability. An out-of-stock response stores no price. |
| `isNew` | Condition. Used offers are skipped unless you enable them per product. |
| `seller.name` | Seller. Needed for the "Amazon as seller only" option. |
| `coupon.label` | Shown for information. Never subtracted. |
| shipping | Not part of this endpoint. Stored as unknown, never assumed free. |

Error handling: `401` and `402` pause the provider for one hour instead of retrying. `429` and `5xx`
are retried with backoff and honour `Retry-After`. A missing product is not retried in a loop: after
three consecutive "not found" results the product is paused.

Canopy does not document an endpoint for actual billing, so the spend shown for Canopy is always
your estimate.

## Apify

Verified against <https://docs.apify.com/api/v2>.

| Step | Request |
|---|---|
| Start a run | `POST /v2/actors/{actorId}/runs?waitForFinish=60&timeout=<seconds>` |
| Poll | `GET /v2/actor-runs/{runId}?waitForFinish=60` |
| Read results | `GET /v2/datasets/{defaultDatasetId}/items` |
| Authentication | Header `Authorization: Bearer <token>` |

There is **no built-in Actor**. Every Actor has its own input, output and price. You choose one and
tell the application how to call it.

Setup:

1. Create an account at <https://apify.com> and copy your API token (Settings > API & Integrations).
2. Pick an Amazon product Actor in the Apify Store. Read its input schema, output example and pricing.
3. Set in `.env`:
   ```ini
   PROVIDER=apify
   DEMO_MODE=false
   APIFY_TOKEN=your-token
   APIFY_ACTOR_ID=username/actor-name
   APIFY_COST_PER_RUN_USD=0.01
   APIFY_MAX_CHARGE_PER_RUN_USD=0.05
   ALLOW_PAID_API_REQUESTS=true
   ```
4. Adjust `APIFY_INPUT_TEMPLATE` and `APIFY_FIELD_MAP` to the Actor.

### Input template

A JSON document sent as the Actor input. These placeholders are replaced inside strings:

| Placeholder | Example |
|---|---|
| `{url}` | `https://www.amazon.com/dp/B0XXXXXXXX` |
| `{asin}` | `B0XXXXXXXX` |
| `{marketplace}` | `US` |
| `{domain}` | `amazon.com` |

### Field map

A JSON object. Keys are this application's fields. Values are dotted paths into one result item.

| Key | Default path | Notes |
|---|---|---|
| `title` | `title` | |
| `brand` | `brand` | |
| `image_url` | `thumbnailImage` | |
| `price` | `price.value` | Required for a valid observation. |
| `currency` | `price.currency` | ISO code, or the symbol of the marketplace currency. |
| `list_price` | `listPrice.value` | |
| `shipping` | `shippingPrice.value` | Missing or null means unknown. |
| `seller` | `seller.name` | |
| `in_stock` | `inStock` | Must be a boolean. |
| `condition` | empty | Empty means the condition is unknown. |

The defaults match the public `junglee/amazon-crawler` Actor as shown on its store page on
2026-11-18 (input field `categoryOrProductUrls`, pay-per-event pricing "from $3.00 / 1,000 results").
Treat this as an example. Check the Actor's current page before you rely on it.

Example for a different Actor:

```ini
APIFY_INPUT_TEMPLATE={"asins": ["{asin}"], "country": "{marketplace}"}
APIFY_FIELD_MAP={"price": "offer.amount", "currency": "offer.currency", "in_stock": "available"}
```

### Cost reporting

Apify returns `usageTotalUsd` for each run. The application stores it as the actual cost of that
request and uses it for the budget. Until a run reports its cost, your estimate is used.
`APIFY_MAX_CHARGE_PER_RUN_USD` is sent as `maxTotalChargeUsd`, a cap that Apify enforces itself.

A failed or timed-out run is not retried within the same check, because a retry would start a second
paid run. The next scheduled check tries again.

## Fallback

```ini
PROVIDER=canopy
FALLBACK_PROVIDER=apify
```

The fallback is tried when the primary provider itself fails (authentication, quota, rate limit,
timeout, network error, server error or malformed response) or has reached its own monthly limit.
It is **not** tried when:

- the primary provider answered that the product does not exist or has no price, or
- the request was skipped because of the global budget, the daily limit or `ALLOW_PAID_API_REQUESTS=false`.

A product can also use a specific provider. Open the product and change **Provider** under
*Monitoring settings*. The stored product and its history stay the same.

## Using several free plans together

Each paid provider can have its own monthly limit. When a provider reaches its limit, the next
provider in the chain takes over. When every provider has reached its limit, checks are skipped
until the next calendar month (UTC). The application stays online.

Example: use Apify's monthly credit first, then Canopy's free requests.

```ini
PROVIDER=apify
FALLBACK_PROVIDER=canopy
ALLOW_PAID_API_REQUESTS=true

APIFY_TOKEN=your-token
APIFY_ACTOR_ID=username/actor-name
APIFY_MAX_MONTHLY_SPEND_USD=5.00       # stop Apify at the free credit

CANOPY_API_KEY=your-key
CANOPY_MAX_MONTHLY_REQUESTS=100        # stop Canopy at the free request count
CANOPY_COST_PER_REQUEST_USD=0          # free requests cost nothing

MAX_MONTHLY_API_SPEND_USD=5.00         # global safety net across all providers
```

| Setting | Counts |
|---|---|
| `<PROVIDER>_MAX_MONTHLY_REQUESTS` | Every request sent to that provider this month, including failed ones and retries. |
| `<PROVIDER>_MAX_MONTHLY_SPEND_USD` | That provider's cost this month: the reported cost when the provider reports it, your estimate otherwise. |

Rules:

- `FALLBACK_PROVIDER` takes one name or a comma-separated list. Providers are tried in that order.
- A provider's own limit moves the check on to the next provider. The global limits
  (`MAX_MONTHLY_API_SPEND_USD`, `MAX_DAILY_API_REQUESTS`, `ALLOW_PAID_API_REQUESTS`) stop the check
  for every provider.
- **Settings** shows, per provider, the requests and cost used this month and its limit.
- Check your free plan's real numbers in the provider's dashboard. Free plans change.
- Set the limit a little below the plan's allowance if you also use the key elsewhere.

How far a free plan goes: requests per month = products x (43,200 / check interval in minutes).
With 100 free requests, that is 3 products checked once a day.

## Spending safeguards

1. **Off by default.** `ALLOW_PAID_API_REQUESTS=false` blocks every paid call.
2. **Reserve before calling.** Each attempt first writes its estimated cost to the database under a
   lock. Parallel checks cannot overspend.
3. **Monthly budget.** A request that would exceed `MAX_MONTHLY_API_SPEND_USD` is not sent.
4. **Daily limit.** `MAX_DAILY_API_REQUESTS` caps paid requests per UTC day.
5. **Failures count.** Failed and retried requests count against the budget.
6. **Persistent.** The ledger is in the database, so limits hold after a restart. A request that was
   interrupted by a restart keeps its reserved cost.
7. **Manual checks obey the same rules.**
8. **One notice.** When the monthly budget stops checks, you get one notification for that month.
   Every skipped check is recorded with its reason under *Activity*.

The dashboard and `GET /api/v1/usage` show requests, spend, the limit and whether the spend figure is
an estimate. Estimated cost and provider-reported cost are stored in separate columns.
