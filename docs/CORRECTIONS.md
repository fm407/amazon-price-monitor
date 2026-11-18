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
| Verification | 224 tests pass inside the Linux container. |
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
