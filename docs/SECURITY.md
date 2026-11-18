# Security

## Scope and threat model

A single person runs this application on their own server. By default it is reachable only from that
server. It may be exposed on a trusted home network. It is not designed to face the public internet.

Protected assets: the provider API keys (they cost money), the Discord webhook URL, the administrator
password, and the integrity of prices and alerts.

## Controls

| Area | Control |
|---|---|
| Network exposure | Published on `127.0.0.1` only by default. No inbound internet access needed. |
| Authentication | Single administrator. Every page and API route requires a session or the API token. Only the health endpoints, `/login` and `/setup` are public. |
| First-run setup | No default password. `/setup` needs a one-time 256-bit token printed in the container log. It is deleted once the account exists. |
| Password storage | scrypt (N=2^15, r=8, p=3) with a random salt per password. Minimum length 12. |
| Brute force | After 5 failed logins or setup attempts, further attempts are refused for 15 minutes. |
| Sessions | Signed cookie, `HttpOnly`, `SameSite=Strict`, optional `Secure`. A new session is issued at login. A password reset invalidates every older session. |
| CSRF | Every state-changing request made with a session must carry the session's CSRF token (form field or `X-CSRF-Token` header). This includes login and setup. |
| REST token | Optional bearer token of at least 32 characters, compared in constant time. |
| Input validation | Pydantic schemas on every API body and form. Unknown fields are rejected. |
| Request size | Bodies larger than 64 KB and multipart uploads are rejected before parsing. |
| SSRF | Only Amazon and three short-link hosts are accepted. Short links are followed by hand, at most 5 hops, https only, no custom ports, no credentials in the URL. Every hop must be an allowlisted host that resolves to a public address. Amazon URLs themselves are parsed, never fetched. |
| SQL injection | All queries use SQLAlchemy with bound parameters. |
| XSS | Jinja2 auto-escaping, no `safe` filter anywhere, and a Content Security Policy without inline scripts or styles. Scripts read data with `textContent`. |
| Headers | `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, `Cache-Control: no-store`. |
| Error handling | Clients get a generic message and a request ID. Details go to the log. Validation errors name the field, not the submitted value. |
| Secrets | Read from the environment or `/run/secrets`. Never stored in the database, never returned by the API, never shown in the dashboard. |
| Stored error text | Provider and delivery error messages are stripped of every configured secret value before they are saved. |
| Logging | Every configured secret value is removed from each log line before it is written. HTTP client logging of request URLs is disabled, because the Discord URL contains its token. |
| Outbound text | Discord messages escape markdown and disable mentions. CSV exports neutralise spreadsheet formulas. Titles are stored as one line. |
| Container | Non-root user, read-only root filesystem, all capabilities dropped, `no-new-privileges`, memory and process limits. No host network, no privileged mode, no Docker socket. |
| Supply chain | Dependencies pinned with hashes in `requirements.txt`. Front-end libraries are vendored and served locally. No analytics, telemetry or third-party scripts. |

## Verification results

Last run: 2026-11-18.

| Check | Command | Result |
|---|---|---|
| Automated tests, including security tests | `./scripts/check.sh` | 335 passed |
| Security lint rules (bandit rule set) | `ruff check .` | no findings |
| Known vulnerabilities in dependencies | `pip-audit -r requirements.txt --disable-pip` | none found |
| Container properties | `./scripts/docker_test.sh` | non-root, read-only filesystem, private secret files, token absent from logs |
| Independent code review | manual, read-only | no critical or high findings; all reported medium and low findings fixed |

Findings of the review and their fixes are listed in [CORRECTIONS.md](CORRECTIONS.md), entry 9.
Tests in `tests/test_hardening.py` keep them fixed.

Security behaviour covered by tests:

- unauthenticated access to every route group
- setup token, password rules, login throttle, session invalidation after reset
- CSRF on forms and API calls
- SSRF: 17 rejected inputs, redirect chains to unsafe targets, private and loopback addresses, redirect loops
- escaping of hostile product titles in every page
- no secret in any API response, stored error or log line
- oversized and multipart bodies

## Accepted risks and residual limitations

- **Plain HTTP.** Acceptable on loopback and through an SSH tunnel. On a LAN, anyone who can observe
  the traffic can read the session cookie. Use a TLS reverse proxy and `SESSION_COOKIE_SECURE=true`
  if that matters to you.
- **Setup token in the log.** It appears once and is useless after setup. Anyone who can read the
  container log before setup could create the account.
- **Secrets in `.env`.** They are visible through `docker inspect` to users who control Docker on the
  host. Mounted secret files avoid that. See [CONFIGURATION.md](CONFIGURATION.md).
- **DNS rebinding.** There is a short window between checking a short-link host's address and
  connecting to it. The fixed host allowlist limits what an attacker could do with it.
- **Login throttle is global.** Someone on your network can lock the login for 15 minutes by guessing
  passwords. This is the trade-off of not locking by client address in a single-user tool.
- **Base image.** `python:3.14-slim` is referenced by tag, not by digest. Rebuild regularly to pick up
  operating system updates.

## Operating advice

- Keep `BIND_ADDRESS=127.0.0.1` unless you need LAN access.
- Run `chmod 600 .env`.
- Rotate a key immediately if it may have leaked: Canopy dashboard, Apify console, or delete and
  recreate the Discord webhook.
- To sign out every session, delete `secret-key` from the data volume and restart, or run
  `python -m app reset-admin`.
- Update with `git pull && docker compose up -d --build`.

## Scanning dependencies yourself

```bash
docker build --target test -t apm-test .
docker run --rm apm-test pip-audit -r requirements.txt --disable-pip
```
