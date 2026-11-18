"""Regression tests for the findings of the security review."""

import pytest
from fastapi.testclient import TestClient

from app import products, security
from app.config import Settings
from app.main import create_app
from tests.conftest import GPU
from tests.test_api import PASSWORD, csrf_from

API = "/api/v1"


@pytest.fixture
def anon(ctx):
    with TestClient(create_app(ctx=ctx), follow_redirects=False) as client:
        yield client


def set_up(client, ctx, password=PASSWORD):
    token = security.setup_token_path(ctx).read_text()
    form = {
        "csrf_token": csrf_from(client.get("/setup").text),
        "token": token,
        "username": "admin",
        "password": password,
        "confirm": password,
    }
    assert client.post("/setup", data=form).status_code == 303
    client.headers["X-CSRF-Token"] = csrf_from(client.get("/").text)


def test_large_and_multipart_bodies_are_rejected_before_parsing(anon):
    big = {"csrf_token": "x", "username": "a" * (security.MAX_BODY_BYTES + 1)}
    assert anon.post("/login", data=big).status_code == 413
    assert anon.post(f"{API}/products", content=b"x" * (security.MAX_BODY_BYTES + 1)).status_code == 413
    no_session = TestClient(anon.app, follow_redirects=False)
    assert no_session.post("/login", files={"upload": ("x.bin", b"data")}).status_code == 403  # no CSRF session at all
    csrf_from(anon.get("/setup").text)  # now the client has a session with a CSRF token
    assert anon.post("/setup", files={"upload": ("x.bin", b"data")}).status_code == 415
    assert anon.post("/login", content=b"abc", headers={"content-length": "abc"}).status_code in (400, 413)


def test_old_session_cookie_is_rejected_after_admin_reset(anon, ctx):
    set_up(anon, ctx)
    assert anon.get("/").status_code == 200 and anon.get(f"{API}/products").status_code == 200
    stolen = dict(anon.cookies)
    security.reset_admin(ctx)
    security.ensure_setup_token(ctx)
    assert anon.get("/").headers["location"] == "/login"  # no administrator: nobody is signed in
    assert anon.get(f"{API}/products").status_code == 401
    fresh = TestClient(anon.app, follow_redirects=False)
    set_up(fresh, ctx, password="a brand new password")
    thief = TestClient(anon.app, follow_redirects=False, cookies=stolen)
    assert thief.get("/").headers["location"] == "/login"
    assert thief.get(f"{API}/products").status_code == 401
    assert fresh.get(f"{API}/products").status_code == 200


def test_short_secrets_are_refused_at_startup():
    for field in ("api_token", "secret_key"):
        with pytest.raises(ValueError, match="at least 32"):
            Settings(_env_file=None, **{field: "changeme"})
    assert Settings(_env_file=None, api_token="x" * 32).api_token


def test_secret_file_is_regenerated_when_empty_or_truncated(tmp_path):
    path = tmp_path / "secret-key"
    path.write_text("")
    first = security._secret_file(path)
    assert len(first) >= security.MIN_SECRET_LENGTH and path.stat().st_mode & 0o777 == 0o600
    assert security._secret_file(path) == first  # stable once valid
    path.write_text("short")
    assert security._secret_file(path) != "short"
    assert not (tmp_path / "secret-key.tmp").exists()


def test_csv_export_neutralises_all_formula_prefixes():
    rows = [{"title": value} for value in ("=1+1", "+1", "-1", "@x", "\t=cmd", "\r=cmd", "", "normal")]
    lines = products.to_csv(rows, ("title",)).splitlines()
    assert lines[1:5] == ["'=1+1", "'+1", "'-1", "'@x"]
    assert products.to_csv([{"title": ""}], ("title",)).splitlines()[1] in ('""', "")  # empty cells stay empty
    assert "'\t=cmd" in products.to_csv([{"title": "\t=cmd"}], ("title",))


def test_titles_are_stored_as_a_single_line(anon, ctx, mock):
    set_up(anon, ctx)
    pid = anon.post(f"{API}/products", json={"input": GPU}).json()["data"]["id"]
    patched = anon.patch(f"{API}/products/{pid}", json={"title": "Line one\r\nBcc: x@example.com\n  two"})
    assert patched.json()["data"]["title"] == "Line one Bcc: x@example.com two"


def test_internal_errors_still_carry_security_headers(ctx, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.api.usage_report", boom)
    ctx.settings.api_token = "x" * 32
    with TestClient(create_app(ctx=ctx), raise_server_exceptions=False) as rest:
        response = rest.get(f"{API}/usage", headers={"Authorization": "Bearer " + "x" * 32})
    assert response.status_code == 500
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
