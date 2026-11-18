"""Authentication, sessions, CSRF, login throttling and first-run setup."""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.context import AppContext
from app.models import ApplicationSetting

log = logging.getLogger(__name__)

# scrypt parameters from the OWASP password storage guidance (N=2^15, r=8, p=3).
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2**15, 8, 3, 32
SCRYPT_MAXMEM = 128 * 1024 * 1024
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
ADMIN_USER_KEY, ADMIN_HASH_KEY = "admin_username", "admin_password_hash"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN, maxmem=SCRYPT_MAXMEM
    )
    encode = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encode(salt)}${encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )  # fmt: skip
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def validate_new_password(password: str, confirm: str) -> str | None:
    if password != confirm:
        return "The passwords do not match."
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        return f"The password must be between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters."
    return None


def get_setting(session: Session, key: str) -> str | None:
    row = session.get(ApplicationSetting, key)
    return row.value if row else None


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(ApplicationSetting, key)
    if row:
        row.value = value
    else:
        session.add(ApplicationSetting(key=key, value=value))


def admin_exists(session: Session) -> bool:
    return get_setting(session, ADMIN_HASH_KEY) is not None


def _secret_file(path: Path) -> str:
    """Read a secret from a 0600 file in the data directory, creating it on first use."""
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
    return value


def session_secret(ctx: AppContext) -> str:
    return ctx.settings.secret_key or _secret_file(ctx.settings.data_dir / "secret-key")


def setup_token_path(ctx: AppContext) -> Path:
    return ctx.settings.data_dir / "setup-token"


def ensure_setup_token(ctx: AppContext) -> str | None:
    """Without an administrator, a one-time token gates /setup. It is shown once in the container log."""
    with ctx.sessions() as session:
        if admin_exists(session):
            setup_token_path(ctx).unlink(missing_ok=True)
            return None
    token = _secret_file(setup_token_path(ctx))
    log.warning(
        "no administrator exists yet: open /setup and enter this one-time setup token", extra={"setup_token": token}
    )
    return token


def create_admin(ctx: AppContext, session: Session, username: str, password: str) -> None:
    set_setting(session, ADMIN_USER_KEY, username)
    set_setting(session, ADMIN_HASH_KEY, hash_password(password))
    session.commit()
    setup_token_path(ctx).unlink(missing_ok=True)
    log.info("administrator account created", extra={"username": username})


def reset_admin(ctx: AppContext) -> None:
    """Recovery path for a lost password: remove the administrator so /setup becomes available again."""
    with ctx.sessions() as session:
        for key in (ADMIN_USER_KEY, ADMIN_HASH_KEY):
            row = session.get(ApplicationSetting, key)
            if row:
                session.delete(row)
        session.commit()


class LoginThrottle:
    """Blocks password guessing: after MAX_FAILURES failures, logins are refused for WINDOW seconds."""

    MAX_FAILURES = 5
    WINDOW = 900.0

    def __init__(self) -> None:
        self._failures: list[float] = []
        self._lock = threading.Lock()

    def blocked(self) -> bool:
        with self._lock:
            cutoff = time.monotonic() - self.WINDOW
            self._failures = [stamp for stamp in self._failures if stamp > cutoff]
            return len(self._failures) >= self.MAX_FAILURES

    def record_failure(self) -> None:
        with self._lock:
            self._failures.append(time.monotonic())

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_session(ctx: AppContext = Depends(get_ctx)) -> Iterator[Session]:
    with ctx.sessions() as session:
        yield session


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(32)
    return token


def log_in(request: Request, username: str) -> None:
    request.session.clear()  # new session on login: prevents session fixation
    request.session["user"] = username
    csrf_token(request)


def _bearer_ok(request: Request, ctx: AppContext) -> bool:
    expected = ctx.settings.api_token
    header = request.headers.get("authorization", "")
    return (
        bool(expected) and header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode(), expected.encode())
    )


def require_user(request: Request, ctx: AppContext = Depends(get_ctx)) -> str:
    """Authenticate by session cookie or, for REST clients, by the optional API bearer token."""
    if _bearer_ok(request, ctx):
        request.state.auth = "token"
        return "api-token"
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    request.state.auth = "session"
    return user


async def require_csrf(request: Request) -> None:
    """State-changing requests made with a session cookie must carry the session's CSRF token."""
    if request.method in SAFE_METHODS or getattr(request.state, "auth", None) == "token":
        return
    supplied = request.headers.get("x-csrf-token")
    content_type = request.headers.get("content-type", "")
    if not supplied and content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        supplied = str((await request.form()).get("csrf_token", ""))
    expected = request.session.get("csrf")
    if not expected or not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token.")


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
