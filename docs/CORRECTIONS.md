# Corrections

Mistakes and wrong assumptions found during development, and how each was fixed.
Requirement changes are listed in [STATUS.md](STATUS.md), not here.

## 1. A milestone was merged with lint and type errors

| | |
|---|---|
| Original implementation | Milestone 2 was gated with a one-line shell command that ran the linter, the type checker and the tests in sequence. |
| Discovered problem | The merge went in with one lint error and one type error. |
| Root cause | The command piped each tool into `tail`, so the exit status of the tool was lost and the chain continued. |
| Correction | Fixed both errors in the next milestone. Added `scripts/check.sh` with `set -euo pipefail`. Every later milestone was merged only after that script passed. |
| Verification | `./scripts/check.sh` exits non-zero on any lint, format, type or test failure. |
| Prevention | One gate script. No ad-hoc gate commands. |

## 2. The migrations folder could shadow the `alembic` package

| | |
|---|---|
| Original implementation | The migration scripts lived in a top-level folder named `alembic/`. |
| Discovered problem | The import sorter classified `import alembic` as a local import. A folder with the package's name in the working directory is an import hazard. |
| Root cause | Folder name equal to a third-party package name. |
| Correction | Renamed the folder to `migrations/` and updated the Dockerfile, `alembic.ini` and the migration runner. |
| Verification | `tests/test_migrations.py` passes locally and in the container. |
| Prevention | No top-level folder shares a name with an installed package. |

## 3. A test asserted the wrong behaviour for an http redirect

| | |
|---|---|
| Original assumption | A short link that redirects to a plain `http://www.amazon.com/...` URL should be accepted. |
| Discovered problem | The implementation rejected it, and the test failed. |
| Root cause | The test was wrong. Requiring https on every hop is the safer behaviour. |
| Correction | Kept the implementation. Changed the test to expect rejection. |
| Verification | `test_short_link_redirect_to_unsafe_destination_is_rejected` passes with the http case in the rejected list. |
| Prevention | When a test and the code disagree, decide which one is right before changing either. |

## 4. Import cycle between alerts, products and the check pipeline

| | |
|---|---|
| Original implementation | `alerts.py` imported the average-price helper from `products.py`, which imports `checker.py`, which imports `alerts.py`. |
| Discovered problem | The test suite could not be collected. |
| Root cause | A helper lived in a higher layer than its only caller. |
| Correction | Moved the helper into `alerts.py`. |
| Verification | Full suite passes. `alerts.py` no longer imports `products.py`. |
| Prevention | Dependencies point one way: routes, services, pipeline, providers. |

## 5. A renamed product lost its title at the next check

| | |
|---|---|
| Original implementation | Every successful check copied the provider's title onto the product. |
| Discovered problem | A dashboard test renamed a product, ran a check, and saw the old title again. |
| Root cause | Provider metadata was treated as always authoritative. |
| Correction | The provider title is applied only while the product still has its placeholder title. After that the title belongs to the user. |
| Verification | `test_product_actions_from_the_dashboard`. |
| Prevention | User-editable fields are filled once, never overwritten by a provider. |

## 6. Blank optional settings crashed the application at startup

| | |
|---|---|
| Original assumption | `KEY=` in `.env` is treated as "not set" for every setting. |
| Discovered problem | Loading `.env.example` failed for `APIFY_MAX_CHARGE_PER_RUN_USD=` and `NOTIFY_MOCK_ENABLED=`. A user following the README would have hit this on first start. |
| Root cause | For optional number and boolean settings, an empty string is not a valid value. |
| Correction | Empty strings are converted to "not set" for those settings. |
| Verification | `test_env_example_documents_every_setting_and_parses` loads the real example file. `docker compose up` with a copy of `.env.example` starts and becomes healthy. |
| Prevention | The example file is loaded by a test, and the same test fails when a setting is added without documenting it. |

## 7. The test image did not contain `.env.example`

| | |
|---|---|
| Original implementation | `.dockerignore` excluded `.env.*`. |
| Discovered problem | Two configuration tests failed inside the container although they passed locally. |
| Root cause | The ignore pattern also matched `.env.example`. |
| Correction | Added an exception for `.env.example` and copied the file into the test stage. |
| Verification | The full suite passes inside the Linux container. |
| Prevention | The suite runs in the container as the first step of `scripts/docker_test.sh`. |

## 8. The Docker script could skip a failed check silently

| | |
|---|---|
| Original implementation | Checks were written as `condition && echo ok`. |
| Discovered problem | The "clean shutdown" line was missing from the output, yet the script reported success. |
| Root cause | Two causes. A false condition in that form does not stop a script. And the check expected exit code 0, while the server shuts down cleanly and then re-raises SIGTERM, which gives 143. |
| Correction | Every check goes through a `check` function that fails loudly. The shutdown check now requires the "application shutdown complete" log line, an exit code of 0 or 143, and a stop within 15 seconds. |
| Verification | `scripts/docker_test.sh` prints both shutdown checks and ends with "All Docker checks passed." |
| Prevention | No bare `&&` checks in scripts. |

## 9. Findings of the independent security review

No critical or high findings. All of the following were fixed and covered by `tests/test_hardening.py`.

| Finding | Root cause | Correction |
|---|---|---|
| A large multipart upload to `/login` could fill memory without authentication | The CSRF check parsed the form body before it validated anything, and file parts had no size limit | Bodies over 64 KB are rejected by middleware. Multipart is refused. The CSRF check fails before parsing when the session has no token. `/tmp` is capped at 64 MB. |
| A stolen session cookie stayed valid after a password reset | Sessions were stateless and not tied to the account | The session stores a fingerprint of the password hash. It is checked on every request. |
| A short `API_TOKEN` or `SECRET_KEY` was accepted | No minimum length | Both must be at least 32 characters, or the application refuses to start. |
| A crash while writing the session key could leave an empty key | The file was created and then written | The key is written to a temporary file and renamed. An empty or short file is replaced. |
| CSV export missed tab and carriage-return formula prefixes, and quoted empty cells | Incomplete prefix list | The full prefix list is used. Empty cells stay empty. |
| A title with a line break made email alerts for that product fail | Titles were stored as entered | Titles are collapsed to one line, from forms and from providers. |
| Error 500 responses lacked the security headers | The last-resort handler runs outside the header middleware | The handler adds the headers itself. |
| No memory or process limits, dependencies without hashes | Not configured | `mem_limit`, `pids_limit`, and hash-pinned requirement files. |

## 10. Apify currency symbol

| | |
|---|---|
| Original assumption | An Actor returns an ISO currency code such as `USD`. |
| Discovered problem | A realistic test item used `"$"`, and the price was rejected for having no valid currency. Some Actors report a symbol. |
| Root cause | The mapping assumed one output convention. |
| Correction | A symbol is accepted only when it is the symbol of the marketplace's own currency (`$` on amazon.com means USD). Any other symbol is still rejected. The currency is never guessed. |
| Verification | `test_currency_symbol_is_accepted_only_for_the_marketplace_currency`. |
| Prevention | Awaiting live verification with a real Actor. See [STATUS.md](STATUS.md). |

## 11. The Docker scenario assumed alerts minutes apart

| | |
|---|---|
| Original assumption | The specification scenario can run unchanged against a live container. |
| Discovered problem | The second alert (after recovery to $750 and a drop to $590) did not fire. |
| Root cause | The checks ran seconds apart, inside the rule's default 60-minute cooldown. The application behaved as designed: the rule stayed armed. |
| Correction | The Docker scenario sets the rule's cooldown to zero first. The unit-level scenario advances a fake clock by six hours per step and keeps the default cooldown. |
| Verification | Both scenarios pass. |
| Prevention | [TESTING.md](TESTING.md) explains the difference. |

## 12. Restore needed write access next to the backup file

| | |
|---|---|
| Original implementation | The restore command opened the backup read-only. |
| Discovered problem | Restoring from a read-only mount failed with "unable to open database file". |
| Root cause | The backup is a WAL-mode database. SQLite wanted to create side files next to it. |
| Correction | The backup is opened as immutable, which reads the file as it is. |
| Verification | Live run: three products, delete one, restore from a read-only mount, three products again. |
| Prevention | The documented backup and restore commands were executed exactly as written before release. |

## 13. Findings of the code review of the provider limit feature

No critical or high findings. All of the following were fixed before the branch was merged.
Regression tests are in `tests/test_free_tier_providers.py`.

| Finding | Root cause | Correction |
|---|---|---|
| A provider's error message was stored as received. A message that echoed the API key would have reached the database and the dashboard | Redaction covered log lines and notification errors, not stored provider text | Provider error detail and the product's last error are stripped of every configured secret before they are saved |
| A product set to "Amazon as seller only" spent Easyparser credits on prices that were always rejected, and never moved to the next provider | Easyparser never names the seller, and a rejected price is not a provider failure | Providers declare whether they report the seller. Those that do not are left out of the chain for such products |
| A "limit reached" skip on a later provider replaced an earlier real error such as a timeout, so the failure was not counted | The last outcome in the chain always won | Outcomes are ranked: a real provider error, then a skip, then "not configured" |
| The fallback price parser for Scrapingdog could misread a decimal comma, a dot used as thousands separator, or a price range | The pattern accepted any digits after the symbol | Only unambiguous US-style amounts are accepted. Everything else yields no price |
| A provider without credentials in the chain reserved budget for a request that was never sent | The reservation happened before the configuration check | Unconfigured providers are skipped before anything is reserved |

## 14. Findings of the code review of the confirmation, pacing and backup features

Two high and four medium findings. All were fixed before the branch was merged.
Regression tests are in `tests/test_trust_and_ops.py`.

| Finding | Root cause | Correction |
|---|---|---|
| High: two unrelated odd readings could confirm each other, even weeks apart, and send an alert | A held reading never expired, and the second reading only had to be "also a large drop" | The second reading must be within 5% of the held one and arrive within 24 hours. A different large drop replaces the held value. An answer without a valid price, and a pause, clear it |
| High: a back-in-stock alert was lost when the returning price was held for confirmation | The stock state was updated before the hold decision, so the confirming check no longer saw "out of stock" | The stock state is left unchanged while a reading is held |
| An answer that did not state the stock erased an earlier "out of stock" | Every answer overwrote the stored stock state | "Unknown" no longer overwrites a stated value. A valid price after "out of stock" counts as back in stock |
| A failed backup left a partial file that blocked retries and could push out a good backup | The backup was written directly under its final name | Backups are written under a temporary name and renamed when complete. A failed attempt leaves nothing behind |
| The provider test on the Settings page could stall the dashboard while a slow provider answered | The route was an async function that called blocking code | It is a plain function that runs in the thread pool, like every other route |
| A skipped check could be recorded under the wrong provider | The ledger entry used the last provider in the chain, not the one the reason belonged to | Each outcome carries its provider, and the entry uses that |
| The back-in-stock message printed a target price and savings | It reused the price-drop message | It shows the current price and the last price before the product went out of stock |
| On-demand backups were deleted by the automatic retention, and could postpone the scheduled backup | Both used the same file name pattern | On-demand backups have their own name and are never deleted automatically |

## 15. Findings of the code review of the alert chart

No critical or high findings. Measured rendering cost: 16 to 25 ms and about 65 KB per chart.
All of the following were fixed before the branch was merged. Regression tests are in
`tests/test_notify.py`.

| Finding | Root cause | Correction |
|---|---|---|
| When Discord refused the chart and the plain retry then hit a rate limit, every later retry uploaded the chart again | The chart was attached on every attempt | Only the first attempt carries the chart. Retries send the alert text alone |
| A history worth less than a few cents produced repeated axis labels | Sub-unit grid steps rounded to the same value | Axis values are made unique |
| Very large prices were cut off at the left edge of the chart | The left margin was fixed | The margin grows to fit the widest price label |
| A product whose currency changed inside the chart window would plot two currencies on one axis | The chart query did not filter by currency | The chart uses observations in the alert's currency only |
| The mock channel kept every message and chart in memory | Unbounded lists | It keeps the latest 200 |
