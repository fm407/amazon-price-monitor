# Status

Last updated: 2026-11-18. Version 1.0.0.

## Milestones

| # | Milestone | Branch | State |
|---|---|---|---|
| 1 | Project structure, Docker setup, application startup | `milestone-1-skeleton` | merged |
| 2 | Database models, migrations, product identity | `milestone-2-data-model` | merged |
| 3 | Provider interface, mock provider, Canopy | `milestone-3-providers` | merged |
| 4 | Scheduler, normalization, price history, budget guard | `milestone-4-scheduler-history` | merged |
| 5 | Alert evaluation and notifications | `milestone-5-alerts-notifications` | merged |
| 6 | Web dashboard, REST API, charts, demo mode | `milestone-6-dashboard-api` | merged |
| 7 | Apify, fallback, cost controls | `milestone-7-apify-cost-controls` | merged |
| 8 | Security hardening, Docker verification, documentation | `milestone-8-hardening-docs` | merged |
| 9 | Per-provider monthly limits, ordered fallback list, SerpApi, Easyparser and Scrapingdog | `feature-per-provider-quotas` | merged |

Each milestone was developed on its own branch and merged into `main` with a merge commit after
`scripts/check.sh` passed. The repository has one maintainer, so merges were verified locally
instead of through pull requests. To repeat the process for a change:

```bash
git checkout -b my-change
./scripts/check.sh && ./scripts/docker_test.sh
git checkout main && git merge --no-ff my-change && git push
```

## Requirement changes during development

| Change | Effect |
|---|---|
| Notifications go to a Discord webhook instead of a Telegram bot | The Telegram channel was removed. `docs/DISCORD_SETUP.md` replaces the Telegram guide. Email stays optional. |
| Custom scheduler loop instead of APScheduler | See "Scheduling" in [ARCHITECTURE.md](ARCHITECTURE.md). The specification allows an equivalent mechanism. |
| Product management was built together with the check pipeline (milestone 4) instead of milestone 2 | Adding a product runs its first check, so the two could not be separated. |
| REST API and authentication were built with the dashboard (milestone 6) | Milestone 8 then covered hardening, the independent review and documentation. |
| No `SchedulerState` table | The schedule is the `next_check_at` column on each product. |

## Acceptance criteria

Verified means: exercised by an automated test or by the Docker script on 2026-11-18.

| Criterion | State | Evidence |
|---|---|---|
| Docker image builds | verified | `scripts/docker_test.sh` |
| Docker Compose starts the application | verified | `docker compose up -d --build` with a copy of `.env.example`, container healthy |
| Dashboard accessible | verified | Screenshots in `docs/images/` taken from the Compose deployment |
| Authentication works | verified | `test_api.py`, `test_hardening.py` |
| Products can be added using Amazon URLs | verified | `test_amazon.py`, `test_web.py`, Docker scenario |
| Duplicates handled | verified | `test_duplicate_product_is_rejected`, Docker scenario |
| Canopy integration implemented | verified against mocked responses | `test_providers.py`. **Awaiting live verification.** |
| Apify integration implemented | verified against mocked responses | `test_apify.py`. **Awaiting live verification.** |
| Mock mode works without credentials | verified | Docker scenario, demo mode |
| Automatic checks | verified | `test_scheduler_checks_only_due_active_products`, scheduler start in the container |
| Manual checks | verified | `test_api.py`, Docker scenario |
| Prices stored persistently | verified | Restart and recreate steps of the Docker script |
| Price charts display | verified | Screenshots, no browser console errors in dark, light and mobile layouts |
| Percentage discount calculations | verified | `test_money.py`, `test_e2e.py` |
| Fixed target prices | verified | `test_fixed_target_alert` |
| Historical-low alerts | verified | `test_new_historical_low_alert` |
| Notifications | Discord and email verified against mocked endpoints | `test_notify.py`. **Awaiting live verification.** The original criterion named Telegram; see the requirement changes above. |
| Duplicate alerts prevented | verified | `test_specification_scenario_including_restart`, Docker scenario |
| API usage recorded | verified | `test_checker.py`, `test_apify.py`, Docker scenario |
| Spending limits enforced | verified | Monthly, daily, concurrent and restart tests in `test_checker.py` |
| Provider failures do not crash the application | verified | `test_provider_exception_does_not_crash_the_check` and the failure-mode tests |
| Container restarts preserve data | verified | Docker script |
| Health checks work | verified | Docker script |
| No secrets exposed | verified | `test_system_endpoints_never_expose_secrets`, log checks |
| Automated tests pass | verified | 297 passed locally and in the container |
| Documentation complete | done | This folder |

## Awaiting live verification

These items need real third-party credentials. The code follows the official documentation and is
tested against mocked HTTP responses, but it has not talked to the real services.

| Item | What to check | How |
|---|---|---|
| Canopy product request | A real response maps to the right price, seller, stock and condition | Set the Canopy settings, add one product, compare with the Amazon page |
| Canopy "product not found" | The documentation lists no dedicated status. The adapter handles a `null` product, HTTP 404 and HTTP 500 | Add a non-existent ASIN and read the error under Activity |
| Canopy rate limiting | HTTP 429 is not in Canopy's documented responses. It is handled generically | Observe during normal use |
| Apify run lifecycle | Start, poll and dataset read against a real Actor | Configure an Actor, add one product |
| Apify field mapping | The default mapping matches the Actor's real output, including the currency | Compare one dataset item in the Apify console with `APIFY_FIELD_MAP` |
| Apify cost reporting | `usageTotalUsd` is present for the chosen Actor's pricing model | Compare Settings with the Apify billing page |
| SerpApi product search | A real response maps to the right price, seller and stock. The seller path was read from a truncated documentation sample | Set the SerpApi settings, add one product, compare with the Amazon page |
| Easyparser DETAIL request | Real envelope fields (`request_info.success`), price and currency | Same procedure |
| Scrapingdog product request | Real field names, and its error statuses, which the documentation does not list | Same procedure |
| Free plan sizes | The allowances in the documentation were read from the providers' pricing pages on 2026-11-18. Canopy's was not confirmed on its own page | Check each provider's dashboard |
| Discord delivery | A real webhook accepts the embed. The image thumbnail renders | Press "Send test notification", then trigger one alert |
| SMTP delivery | Your mail server accepts the message | Configure SMTP and press "Send test notification" |
| Short-link resolution | `a.co` and `amzn.to` redirect chains end on a product page through allowed hosts only | Add a product with a short link |

The Discord documentation site was not reachable from the development network. The webhook request
format was taken from Discord's official documentation repository on GitHub instead.

## Known limitations

- When several providers serve one product, small differences between their prices can look like price changes.
- SerpApi may serve a result that is up to one hour old from its cache.
- Easyparser never names the seller, so it is skipped for products set to "Amazon as seller only".
- SerpApi and Scrapingdog report no shipping cost and no currency code. The currency is taken from the symbol only when it matches the marketplace.
- Only the `amazon.com` marketplace is enabled.
- Canopy's product endpoint has no shipping cost. Canopy prices are item prices with shipping unknown.
- Seller filtering depends on the provider naming the seller. Without a name, "Amazon as seller only" skips the price.
- The price is the single offer the provider returns (the Buy Box). Other offers are not compared.
- Coupons, lightning deals and tax are not applied to the price.
- Canopy spend is always an estimate.
- Changing configuration needs a restart. There is no settings editor in the dashboard.
- One administrator account.
- The image is about 300 MB, of which about 200 MB is the Python base image.
- Screenshots were taken with Chrome only. Firefox and Safari were not tested.

## Not built in this release

Multiple marketplaces, other retailers, a browser extension, watchlists, multiple recipients, web
push, quiet hours, price trend analysis, product comparison, historical price import and PostgreSQL.
[ARCHITECTURE.md](ARCHITECTURE.md) lists where each would attach.
