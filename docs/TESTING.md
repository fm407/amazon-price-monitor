# Testing

All tests are deterministic. They use a fake clock, a seeded random generator and mocked HTTP.
No test needs a paid API, a real Discord webhook or network access. Any unexpected outbound request
fails the test.

## Run the tests

In Docker (no local Python needed):

```bash
./scripts/docker_test.sh
```

This script:

1. builds a test image and runs the whole test suite inside a Linux container;
2. builds the runtime image;
3. starts a disposable container with a read-only filesystem and a temporary volume;
4. checks health, the non-root user, volume permissions and scheduler start;
5. runs the end-to-end scenario through the REST API with the mock provider;
6. restarts the container and checks that data and alert state persist;
7. recreates the container on the same volume, as an image upgrade would;
8. checks that the API token is absent from the logs and that shutdown is graceful.

It uses its own container, volume and port (18080). It never touches your real deployment.

Only the test suite:

```bash
docker build --target test -t apm-test . && docker run --rm apm-test
```

Locally, with [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
./scripts/check.sh                      # lint, format check, type check, tests
./scripts/check.sh --cov=app            # with coverage
```

`scripts/check.sh` stops at the first failing step.

## Latest results

Run on 2026-11-18.

| Check | Result |
|---|---|
| `ruff check` (includes security rules) | passed |
| `ruff format --check` | passed |
| `mypy` | passed, 32 source files |
| `pytest` on macOS, Python 3.14 | 335 passed |
| `pytest` in the Linux container, Python 3.14 | 335 passed |
| Line coverage | 95% |
| `pip-audit` | no known vulnerabilities |
| `scripts/docker_test.sh` | all checks passed |

## What the tests cover

| File | Kind | Covers |
|---|---|---|
| `test_amazon.py` | unit | ASIN extraction, URL normalization, rejected inputs, short links, SSRF protections |
| `test_money.py` | unit | Decimal parsing, invalid numbers, minor units, percentages, the $800 to $600 example |
| `test_alerts.py` | unit | Threshold encoding, crossing, cooldown, reminders, further drops, event rules |
| `test_providers.py` | unit | Canopy request and mapping, every error category, the mock provider |
| `test_apify.py` | unit and integration | Run lifecycle, failed runs, timeouts, field mapping, fallback, reported cost, budget notice |
| `test_free_tier_providers.py` | unit and integration | SerpApi, Easyparser and Scrapingdog requests and mapping, shared error handling, strict amount parsing, key redaction, seller capability, three free plans used in turn |
| `test_quotas.py` | integration | Per-provider monthly limits, spill-over to the next provider, restart persistence, interaction with the global budget |
| `test_trust_and_ops.py` | integration | Provider test action, confirmation of large drops, pacing of monthly limits, back-in-stock alerts, scheduled backups |
| `test_checker.py` | integration | Product creation, history, invalid responses, shipping, retries, rate limits, concurrency, deletion during a check, scheduler, restart recovery, budget and daily limits |
| `test_e2e.py` | end to end | The specification scenario including a restart, and one scenario per alert type |
| `test_notify.py` | integration | Discord payload, escaping, retries, permanent failures, secret redaction, email, the price history chart and its fallbacks |
| `test_api.py` | integration | Authentication, setup, CSRF, the REST API, exports, error format, no secrets in responses |
| `test_web.py` | integration | Every page and form, output escaping, demo mode |
| `test_hardening.py` | integration | Body limits, session invalidation, secret rules, CSV and title sanitising |
| `test_migrations.py` | integration | Migrations build a schema identical to the models, and an upgrade keeps existing data |
| `test_config.py` | unit | `.env.example` matches the settings, safe defaults, backup and restore |

## The specification scenario

`tests/test_e2e.py::test_specification_scenario_including_restart` runs these steps through the real
pipeline. `scripts/docker_test.sh` repeats them against a running container.

| Step | Price | Expected | 
|---|---|---|
| Add product, 25% rule | $800 | baseline $800, target $600 |
| Check | $700 | no alert |
| Check | $599 | alert sent |
| Check | $599 | no duplicate |
| Check | $750 | no alert, rule re-armed |
| Check | $590 | new alert |
| Restart, check | $590 | no duplicate |
| Check | $800, then $580 | new alert |

In the Docker run the checks are seconds apart, so the script sets the rule's cooldown to zero first.
With the default 60-minute cooldown and real check intervals of hours, the same sequence produces the
same alerts.

## Writing a test

`tests/conftest.py` provides `ctx` (an application context on a temporary database with a fake clock)
and `mock` (the mock provider).

```python
def test_example(ctx, mock):
    with ctx.sessions() as session:
        product, _ = products.add_product(ctx, session, "B0DEMOGPU1")
    ctx.now.advance(hours=6)
    mock.set_price("B0DEMOGPU1", "599.00")
    assert check_product(ctx, product.id).status == "ok"
```

For a paid provider, pass an `httpx.MockTransport` handler to `make_ctx` and return canned responses.
