"""REST API, authentication, CSRF and security behaviour through the real ASGI app."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import security
from app.config import Settings
from app.main import create_app
from app.models import Product
from tests.conftest import GPU, make_ctx

PASSWORD = "correct horse battery"
API = "/api/v1"


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def anon(ctx):
    with TestClient(create_app(ctx=ctx), follow_redirects=False) as client:
        yield client


@pytest.fixture
def client(anon, ctx):
    """A signed-in client whose default headers carry the session CSRF token."""
    token = security.setup_token_path(ctx).read_text()
    page = anon.get("/setup")
    form = {
        "csrf_token": csrf_from(page.text),
        "token": token,
        "username": "admin",
        "password": PASSWORD,
        "confirm": PASSWORD,
    }
    assert anon.post("/setup", data=form).status_code == 303
    anon.headers["X-CSRF-Token"] = csrf_from(anon.get("/").text)
    return anon


def create(client, text=GPU, **extra):
    return client.post(f"{API}/products", json={"input": text, **extra})


# --- Authentication and setup --------------------------------------------------------------------


def test_health_is_public_and_everything_else_requires_authentication(anon):
    assert anon.get(f"{API}/health").json()["data"]["status"] == "ok"
    assert anon.get(f"{API}/health/live").status_code == 200
    for path in (
        "/products",
        "/alerts",
        "/notifications",
        "/providers",
        "/usage",
        "/settings",
        "/openapi.json",
        "/export/products",
    ):
        response = anon.get(API + path)
        assert response.status_code == 401, path
        assert response.json() == {
            "success": False, "data": None, "meta": None,
            "error": {"code": "unauthorized", "message": "Authentication required.", "details": None},
        }  # fmt: skip
    assert anon.post(f"{API}/products", json={"input": GPU}).status_code == 401
    for path in ("/", "/activity", "/settings", "/products/1"):
        assert anon.get(path).headers["location"] == "/login"
    assert anon.get("/login").headers["location"] == "/setup"
    assert anon.get("/docs").status_code == 404


def test_setup_requires_the_one_time_token_and_runs_only_once(anon, ctx):
    token_path = security.setup_token_path(ctx)
    assert token_path.stat().st_mode & 0o777 == 0o600
    csrf = csrf_from(anon.get("/setup").text)
    base = {"csrf_token": csrf, "username": "admin", "password": PASSWORD, "confirm": PASSWORD}
    assert anon.post("/setup", data={**base, "token": "wrong"}).status_code == 400
    assert (
        anon.post(
            "/setup", data={**base, "token": token_path.read_text(), "password": "short", "confirm": "short"}
        ).status_code
        == 400
    )
    assert (
        anon.post("/setup", data={**base, "token": token_path.read_text(), "confirm": "different password"}).status_code
        == 400
    )
    assert (
        anon.post("/setup", data={"username": "admin", "token": token_path.read_text()}).status_code == 403
    )  # no CSRF token
    assert anon.post("/setup", data={**base, "token": token_path.read_text()}).status_code == 303
    assert not token_path.exists()
    with ctx.sessions() as session:
        stored = security.get_setting(session, security.ADMIN_HASH_KEY)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert anon.get("/setup").headers["location"] == "/login"
    fresh = {**base, "csrf_token": csrf_from(anon.get("/").text), "token": "anything"}  # login rotated the CSRF token
    assert anon.post("/setup", data=fresh).headers["location"] == "/login"


def test_login_logout_and_brute_force_throttle(client, ctx):
    assert client.post("/logout", data={}).status_code == 303
    assert client.get("/").headers["location"] == "/login"
    csrf = csrf_from(client.get("/login").text)
    client.headers.pop("X-CSRF-Token")
    bad = {"csrf_token": csrf, "username": "admin", "password": "wrong password"}
    assert client.post("/login", data=bad).status_code == 401
    assert client.post("/login", data={**bad, "username": "nobody", "password": PASSWORD}).status_code == 401
    good = {"csrf_token": csrf, "username": "admin", "password": PASSWORD}
    assert client.post("/login", data=good).status_code == 303
    assert client.get("/").status_code == 200
    client.post("/logout", data={"csrf_token": csrf_from(client.get("/").text)})
    csrf = csrf_from(client.get("/login").text)
    for _ in range(security.LoginThrottle.MAX_FAILURES):
        client.post("/login", data={**bad, "csrf_token": csrf})
    assert (
        client.post("/login", data={**good, "csrf_token": csrf}).status_code == 429
    )  # locked even with the right password


def test_session_cookie_flags_and_security_headers(client):
    response = client.get("/")
    cookie = client.get("/login", headers={"cookie": ""}).headers.get("set-cookie", "") or response.headers.get(
        "set-cookie", ""
    )
    assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"].startswith("req-")
    assert "server" not in response.headers


def test_state_changing_requests_need_the_csrf_token(client):
    token = client.headers.pop("X-CSRF-Token")
    response = create(client)
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"
    assert create(client, headers=None).status_code == 403
    client.headers["X-CSRF-Token"] = "forged"
    assert create(client).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert create(client).status_code == 201


def test_bearer_token_authenticates_rest_clients(tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, scheduler_enabled=False, api_token="rest-client-token-0123456789abcdef"
    )
    ctx = make_ctx(settings)
    with TestClient(create_app(ctx=ctx)) as rest:
        assert rest.get(f"{API}/products", headers={"Authorization": "Bearer wrong"}).status_code == 401
        auth = {"Authorization": "Bearer rest-client-token-0123456789abcdef"}
        assert rest.post(f"{API}/products", json={"input": GPU}, headers=auth).status_code == 201  # no CSRF needed
        assert rest.get(f"{API}/products", headers=auth).json()["meta"]["total"] == 1
    ctx.close()


def test_production_password_hash_parameters(monkeypatch):
    monkeypatch.undo()
    stored = security.hash_password(PASSWORD)
    assert stored.startswith("scrypt$32768$8$3$")
    assert security.verify_password(PASSWORD, stored)
    assert not security.verify_password(PASSWORD + "x", stored)
    assert not security.verify_password(PASSWORD, "not-a-hash")
    assert security.hash_password(PASSWORD) != stored  # random salt


def test_reset_admin_reopens_setup(client, ctx):
    security.reset_admin(ctx)
    assert security.ensure_setup_token(ctx)
    assert client.get("/login").headers["location"] == "/setup"


# --- Products ----------------------------------------------------------------------------------


def test_product_lifecycle(client, mock):
    created = create(client, "https://www.amazon.com/dp/B0DEMOGPU1?tag=x", discount_percent="25", target_price="550")
    assert created.status_code == 201
    product = created.json()["data"]
    assert (product["asin"], product["url"]) == (GPU, "https://www.amazon.com/dp/B0DEMOGPU1")
    assert (product["baseline_price"], product["latest_price"], product["target_price"]) == (
        "800.00",
        "800.00",
        "600.00",
    )
    assert [a["kind"] for a in product["alerts"]] == ["pct_below_baseline", "fixed_target"]
    pid = product["id"]

    duplicate = create(client)
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "duplicate_product"

    mock.set_price(GPU, "640.00")
    check = client.post(f"{API}/products/{pid}/check").json()["data"]
    assert (check["status"], check["provider"]) == ("ok", "mock")
    fetched = client.get(f"{API}/products/{pid}").json()["data"]
    assert (fetched["latest_price"], fetched["previous_price"], fetched["change_from_baseline_percent"]) == (
        "640.00",
        "800.00",
        "-20.00",
    )

    patched = client.patch(
        f"{API}/products/{pid}", json={"check_interval_minutes": 60, "seller_filter": "amazon", "title": "My GPU"}
    )
    assert patched.json()["data"]["check_interval_minutes"] == 60 and patched.json()["data"]["title"] == "My GPU"
    assert (
        client.patch(f"{API}/products/{pid}", json={"provider": "canopy"}).status_code == 422
    )  # not registered in mock mode
    assert client.post(f"{API}/products/{pid}/pause").json()["data"]["status"] == "paused"
    assert client.post(f"{API}/products/{pid}/resume").json()["data"]["status"] == "active"
    assert client.post(f"{API}/products/{pid}/baseline", json={}).json()["data"]["baseline_price"] == "640.00"
    assert (
        client.post(f"{API}/products/{pid}/baseline", json={"price": "700"}).json()["data"]["target_price"] == "525.00"
    )

    history = client.get(f"{API}/products/{pid}/history?range=7d").json()
    assert history["meta"]["total"] == 2
    assert [o["price"] for o in history["data"]["observations"]] == ["800.00", "640.00"]
    assert history["data"]["baseline_price"] == "700.00"

    assert client.delete(f"{API}/products/{pid}").json()["data"] == {"deleted": pid}
    assert client.get(f"{API}/products/{pid}").status_code == 404
    assert client.get(f"{API}/alerts").json()["meta"]["total"] == 0  # rules are deleted with the product


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"input": "https://example.com/dp/B0DEMOGPU1"}, 422, "invalid_product_input"),
        ({"input": "http://169.254.169.254/latest/meta-data"}, 422, "invalid_product_input"),
        ({"input": "short"}, 422, "validation_error"),
        ({"input": GPU, "discount_percent": 150}, 422, "validation_error"),
        ({"input": GPU, "target_price": -1}, 422, "validation_error"),
        ({"input": GPU, "unexpected": True}, 422, "validation_error"),
        ({}, 422, "validation_error"),
    ],
)
def test_invalid_product_requests_use_the_error_envelope(client, body, status, code):
    response = client.post(f"{API}/products", json=body)
    payload = response.json()
    assert response.status_code == status and payload["success"] is False and payload["error"]["code"] == code
    assert "169.254" not in str(payload) or code == "invalid_product_input"


def test_unknown_product_from_provider_is_404(client, mock):
    mock.set_mode("not_found", "B0MISSING1")
    response = create(client, "B0MISSING1")
    assert response.status_code == 404 and response.json()["error"]["code"] == "product_not_found"


def test_list_supports_pagination_filter_and_sort(client, mock):
    for asin, price in (("B0DEMOGPU1", "800"), ("B0DEMOSSD1", "260"), ("B0DEMORAM1", "140")):
        mock.set_price(asin, price)
        assert create(client, asin, discount_percent="10").status_code == 201
    mock.set_price("B0DEMOSSD1", "200")
    ssd = client.get(f"{API}/products?q=ssd").json()["data"][0]
    client.post(f"{API}/products/{ssd['id']}/check")
    first = client.get(f"{API}/products?limit=2&page=1&sort=price").json()
    assert first["meta"] == {"total": 3, "page": 1, "limit": 2}
    assert [p["latest_price"] for p in first["data"]] == ["140.00", "200.00"]
    assert len(client.get(f"{API}/products?limit=2&page=2").json()["data"]) == 1
    assert [p["asin"] for p in client.get(f"{API}/products?sort=discount").json()["data"]][0] == "B0DEMOSSD1"
    assert [p["asin"] for p in client.get(f"{API}/products?status=below").json()["data"]] == ["B0DEMOSSD1"]
    assert client.get(f"{API}/products?status=paused").json()["meta"]["total"] == 0
    assert client.get(f"{API}/products?limit=5000").status_code == 422
    assert client.get(f"{API}/products?sort=bogus").status_code == 422


def test_manual_check_reports_busy_as_conflict(client, ctx):
    pid = create(client).json()["data"]["id"]
    assert ctx.try_begin_check(pid)
    response = client.post(f"{API}/products/{pid}/check")
    assert response.status_code == 409 and response.json()["error"]["code"] == "conflict"
    ctx.end_check(pid)


def test_exports(client, ctx):
    pid = create(client).json()["data"]["id"]
    with ctx.sessions() as session:
        session.get(Product, pid).title = "=HYPERLINK(evil)"
        session.commit()
    csv_response = client.get(f"{API}/export/products?format=csv")
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_response.headers["content-disposition"]
    assert "'=HYPERLINK(evil)" in csv_response.text and "B0DEMOGPU1" in csv_response.text
    assert client.get(f"{API}/export/products?format=json").json()["data"][0]["latest_price"] == "800.00"
    history_csv = client.get(f"{API}/products/{pid}/history/export?format=csv").text.splitlines()
    assert history_csv[0].startswith("observed_at,price,item_price,shipping") and "800.00" in history_csv[1]
    assert client.get(f"{API}/products/{pid}/history/export?format=json").json()["data"][0]["provider"] == "mock"


# --- Alerts, notifications, system -------------------------------------------------------------


def test_alert_rule_crud_and_notification_history(client, mock):
    pid = create(client).json()["data"]["id"]
    created = client.post(
        f"{API}/alerts",
        json={"product_id": pid, "kind": "pct_below_baseline", "threshold": "25", "cooldown_minutes": 0},
    )
    assert created.status_code == 201
    rule = created.json()["data"]
    assert (rule["description"], rule["threshold"], rule["is_below"]) == ("25% below baseline", "25", False)
    assert client.post(f"{API}/alerts", json={"product_id": pid, "kind": "fixed_target"}).status_code == 422
    assert client.post(f"{API}/alerts", json={"product_id": 999, "kind": "new_low"}).status_code == 404
    assert client.post(f"{API}/alerts", json={"product_id": pid, "kind": "bogus", "threshold": 5}).status_code == 422

    mock.set_price(GPU, "599.00")
    client.post(f"{API}/products/{pid}/check")
    events = client.get(f"{API}/alert-events?product_id={pid}").json()
    assert events["meta"]["total"] == 1
    assert (events["data"][0]["price"], events["data"][0]["target_price"], events["data"][0]["event_type"]) == (
        "599.00",
        "600.00",
        "trigger",
    )
    deliveries = client.get(f"{API}/notifications").json()["data"]
    assert [(d["channel"], d["status"]) for d in deliveries] == [("mock", "sent")]
    assert client.get(f"{API}/alerts?product_id={pid}").json()["data"][0]["is_below"] is True

    updated = client.patch(
        f"{API}/alerts/{rule['id']}", json={"threshold": "30", "enabled": False, "reminder_minutes": 720}
    ).json()["data"]
    assert (updated["threshold"], updated["enabled"], updated["reminder_minutes"], updated["is_below"]) == (
        "30",
        False,
        720,
        False,
    )
    assert client.patch(f"{API}/alerts/{rule['id']}", json={"threshold": "0"}).status_code == 422
    assert client.delete(f"{API}/alerts/{rule['id']}").status_code == 200
    assert client.patch(f"{API}/alerts/{rule['id']}", json={"enabled": True}).status_code == 404
    assert client.get(f"{API}/alert-events").json()["meta"]["total"] == 1  # history outlives the rule


def test_test_notification_endpoint(client, ctx):
    result = client.post(f"{API}/notifications/test").json()["data"]
    assert [(d["channel"], d["status"]) for d in result] == [("mock", "sent")]
    ctx.notifiers.clear()
    assert client.post(f"{API}/notifications/test").status_code == 409


def test_system_endpoints_never_expose_secrets(tmp_path):
    secrets = {
        "secret_key": "session-secret-value-0123456789abcdef", "api_token": "api-token-value-0123456789abcdef-22",
        "canopy_api_key": "canopy-key-value-333",
        "apify_token": "apify-token-value-4444", "smtp_password": "smtp-password-55555",
        "discord_webhook_url": "https://discord.com/api/webhooks/42/discord-token-666666",
    }  # fmt: skip
    settings = Settings(
        _env_file=None, data_dir=tmp_path, scheduler_enabled=False, smtp_host="smtp.example.com",
        smtp_from="a@example.com", smtp_to="b@example.com", **secrets,
    )  # fmt: skip
    ctx = make_ctx(settings)
    auth = {"Authorization": "Bearer api-token-value-0123456789abcdef-22"}
    with TestClient(create_app(ctx=ctx)) as rest:
        rest.post(f"{API}/products", json={"input": GPU}, headers=auth)
        bodies = [
            rest.get(API + path, headers=auth).text
            for path in (
                "/settings",
                "/providers",
                "/usage",
                "/usage/requests",
                "/health",
                "/openapi.json",
                "/products",
            )
        ]
        info = rest.get(f"{API}/settings", headers=auth).json()["data"]
    assert (info["discord"], info["smtp"], info["api_token"]) == ("configured", "configured", "configured")
    for body in bodies:
        for value in (*secrets.values(), "discord-token-666666"):
            assert value not in body
    ctx.close()


def test_usage_providers_and_openapi(client):
    create(client)
    usage = client.get(f"{API}/usage").json()["data"]
    assert usage["requests"] == 1 and usage["cost_usd"] == "0.0000" and usage["cost_is_estimate"] is True
    assert usage["paid_requests_allowed"] is False and usage["by_provider"][0]["provider"] == "mock"
    [provider] = client.get(f"{API}/providers").json()["data"]
    assert (provider["name"], provider["role"], provider["health"], provider["paid"]) == (
        "mock",
        "primary",
        "ok",
        False,
    )
    assert client.get(f"{API}/usage/requests?failed_only=true").json()["meta"]["total"] == 0
    schema = client.get(f"{API}/openapi.json").json()
    for path in (
        "/api/v1/products",
        "/api/v1/products/{product_id}/check",
        "/api/v1/alerts/{alert_id}",
        "/api/v1/notifications/test",
        "/api/v1/usage",
    ):
        assert path in schema["paths"], path


def test_mock_control_endpoint_simulates_prices_and_failures(client):
    pid = create(client).json()["data"]["id"]
    assert (
        client.post(f"{API}/mock/products/{GPU}", json={"price": "555.55", "seller": "Demo Seller"}).status_code == 200
    )
    client.post(f"{API}/products/{pid}/check")
    product = client.get(f"{API}/products/{pid}").json()["data"]
    assert (product["latest_price"], product["latest_seller"]) == ("555.55", "Demo Seller")
    client.post(f"{API}/mock/products/{GPU}", json={"mode": "outage"})
    assert client.post(f"{API}/products/{pid}/check").json()["data"]["category"] == "network"
    assert client.get(f"{API}/products/{pid}").json()["data"]["latest_price"] == "555.55"
    assert client.post(f"{API}/mock/products/{GPU}", json={"mode": "bogus"}).status_code == 422


def test_unhandled_errors_are_sanitised(ctx, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail /app/data/monitor.db")

    monkeypatch.setattr("app.api.usage_report", boom)
    settings_token = "rest-client-token-0123456789abcdef"
    ctx.settings.api_token = settings_token
    with TestClient(create_app(ctx=ctx), raise_server_exceptions=False) as rest:
        response = rest.get(f"{API}/usage", headers={"Authorization": f"Bearer {settings_token}"})
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": "Internal error. The details are in the application log.",
        "details": None,
    }


def test_deleted_product_rows(client, ctx):
    pid = create(client).json()["data"]["id"]
    client.delete(f"{API}/products/{pid}")
    with ctx.sessions() as session:
        assert session.scalars(select(Product)).all() == []
