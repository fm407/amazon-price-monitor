# Price providers

The application never scrapes Amazon itself. It asks a provider for product data.
Every provider implements one interface: `fetch_product(asin, marketplace)`.

| Provider | Account needed | Free plan seen on 2026-11-18 | One check costs |
|---|---|---|---|
| `mock` | no | always free | nothing. Fictional prices for demos and tests. |
| `serpapi` | yes | 250 searches per month, no card | 1 search |
| `easyparser` | yes | 100 credits per month, no card | 1 credit |
| `scrapingdog` | yes | 100 credits per month, no card | 1 credit |
| `apify` | yes | $5 of platform usage per month, no card | 1 Actor run, priced by the Actor |
| `canopy` | yes | reported as 100 requests per month (not confirmed on the provider's own page) | 1 request |

Free plans change. Check the provider's pricing page before you rely on these numbers.
You can combine several providers and their free plans. See "Using several free plans together".

Each stored price records which provider supplied it.

## Test a provider

After you set a key, check it before you rely on it.

1. Open **Settings**.
2. Under *Test a provider*, choose the provider and paste an Amazon link or ASIN.
3. Press **Send one test request**.

The result shows the title, price, currency, shipping, seller, condition and availability exactly as
the provider returned them, and whether the application would record that price. Compare it with the
Amazon page. Nothing is saved. The request counts against the limits like any other.

The same test is available as `POST /api/v1/providers/{name}/test` with `{"input": "<link or ASIN>"}`.

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

## SerpApi

Verified against <https://serpapi.com/amazon-product-api> and
<https://serpapi.com/api-status-and-error-codes>.

| Item | Value |
|---|---|
| Request | `GET https://serpapi.com/search?engine=amazon_product&asin=<ASIN>&amazon_domain=amazon.com&api_key=<key>` |
| Price | `purchase_options.buy_new.extracted_price`, else `product_results.extracted_price` |
| Seller | `purchase_options.buy_new.features.shipper_seller.text` |
| Stock | `product_results.stock` |
| Currency | No currency code. The symbol in the price text is accepted only when it is the marketplace currency. |
| Shipping | Unknown. The delivery text is conditional ("over $35"). |

```ini
SERPAPI_API_KEY=your-key
SERPAPI_COST_PER_REQUEST_USD=0
SERPAPI_MAX_MONTHLY_REQUESTS=250
```

SerpApi answers a repeated search from a one-hour cache without charging for it. A price can
therefore be up to one hour old. When the price comes from the "buy new" offer the condition is
`new`; otherwise it is `unknown`.

## Easyparser

Verified against <https://docs.easyparser.com/real-time-integration/overview> and its DETAIL
response page.

| Item | Value |
|---|---|
| Request | `GET https://realtime.easyparser.com/v1/request?api_key=<key>&platform=AMZ&operation=DETAIL&domain=.com&asin=<ASIN>` |
| Price | `result.detail.buybox_winner.price.value` with `.currency` (ISO code) |
| Condition | `buybox_winner.condition.is_new` |
| Stock | `buybox_winner.availability.raw` |
| Shipping | `buybox_winner.shipping.raw`. Only the exact text `FREE` is read as zero. Anything else is unknown. |
| Seller | Not available. The response gives a seller type, not a name. |

```ini
EASYPARSER_API_KEY=your-key
EASYPARSER_COST_PER_REQUEST_USD=0
EASYPARSER_MAX_MONTHLY_REQUESTS=100
```

The adapter checks `request_info.success` even when the HTTP status is 200, as the documentation
requires. Requests use a 75-second timeout because the documentation recommends at least 60.
Because there is no seller name, Easyparser is left out automatically for products set to
"Amazon as seller only". Those products use the other providers in your list.

## Scrapingdog

Verified against <https://www.scrapingdog.com/documentation/amazon-product-scraper/>.

| Item | Value |
|---|---|
| Request | `GET https://api.scrapingdog.com/amazon/product?api_key=<key>&asin=<ASIN>&domain=com&country=us` |
| Price | `purchase_options.single_offer.extracted_price`. If absent, the number at the start of the `price` text. |
| Seller | `sold_by` |
| Stock | `availability_status` |
| Currency | `price_symbol` only. Accepted when it is the marketplace currency. |
| Shipping | Unknown. `shipping_info` is free text. |
| Condition | Unknown. The response has no condition field. |

```ini
SCRAPINGDOG_API_KEY=your-key
SCRAPINGDOG_COST_PER_REQUEST_USD=0
SCRAPINGDOG_MAX_MONTHLY_REQUESTS=100
```

The documentation page lists no error statuses. The adapter applies the same handling as for the
other providers: 401 and 403 pause the provider, 429 and 5xx are retried, 404 means not found.

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

Example: three free plans, about 450 checks per month in total.

```ini
PROVIDER=serpapi
FALLBACK_PROVIDER=easyparser,scrapingdog
ALLOW_PAID_API_REQUESTS=true
DEMO_MODE=false

SERPAPI_API_KEY=your-key
SERPAPI_COST_PER_REQUEST_USD=0
SERPAPI_MAX_MONTHLY_REQUESTS=250

EASYPARSER_API_KEY=your-key
EASYPARSER_COST_PER_REQUEST_USD=0
EASYPARSER_MAX_MONTHLY_REQUESTS=100

SCRAPINGDOG_API_KEY=your-key
SCRAPINGDOG_COST_PER_REQUEST_USD=0
SCRAPINGDOG_MAX_MONTHLY_REQUESTS=100

MAX_MONTHLY_API_SPEND_USD=1.00         # global safety net; free requests cost nothing against it
MAX_DAILY_API_REQUESTS=15              # 450 per month is 15 per day
DEFAULT_CHECK_INTERVAL_MINUTES=720     # 7 products, twice a day
```

Apify and Canopy can join the same list. For Apify, limit by spend instead of by request count:
`APIFY_MAX_MONTHLY_SPEND_USD=5.00`.

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

### Pacing

With `PACE_MONTHLY_LIMITS=true` (the default), a provider's monthly limit is not used up in the first
days. At any moment a provider may have used the elapsed share of the month plus 10% of its limit.
A provider that is ahead of that pace hands over to the next one and is used again as the month goes
on. Example: with a limit of 250, about 25 requests are available on the first day and about 8 more
each day after that. Set `PACE_MONTHLY_LIMITS=false` to use each limit as fast as checks arrive.

When every provider is at its limit or ahead of its pace, the check is skipped and recorded once
under **Activity**. It runs again at its next scheduled time.

### Confirming large drops

With `CONFIRM_DROP_PERCENT=15`, a price that is 15% or more below the last stored price is not
recorded at once. It is held, and a second check runs after `CONFIRM_RECHECK_MINUTES` (15 by
default), from a different provider when one is available.

- If the second reading is also a large drop, it is recorded and alerts are evaluated as usual.
- If the second reading is normal, the held value is discarded. No alert is sent.

The product page shows "Waiting for confirmation" while a value is held. The cost is one extra
request per large drop and a delay of a few minutes for real deals. The first price of a new
product is never held.

Things to know when several providers serve the same product:

- Providers can report slightly different prices for the same product at the same moment, for
  example with or without shipping. A switch between providers can look like a small price change.
  Prefer alert thresholds of several percent over very tight ones.
- A provider that never names the seller (Easyparser) is skipped for products set to "Amazon as
  seller only", so its allowance is not spent on prices that would be rejected.
- Every observation records its provider, so you can see the source of each point in the history.

How far a free plan goes: requests per month = products x (43,200 / check interval in minutes).
With 450 free requests, that is 15 products once a day, or 7 products twice a day.

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
