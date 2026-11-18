"""Scenario steps for scripts/docker_test.sh. Runs inside the container against the local API."""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080/api/v1"
TOKEN = "docker-test-token-0001-not-a-real-secret"  # noqa: S105 - throwaway value for the disposable test container
ASIN = "B0DEMOGPU1"


def call(method: str, path: str, body: dict | None = None, token: str | None = TOKEN) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def expect(label: str, actual: object, wanted: object) -> None:
    if actual != wanted:
        sys.exit(f"FAIL {label}: expected {wanted!r}, got {actual!r}")
    sys.stdout.write(f"  ok  {label}\n")


def price(product_id: int, value: str) -> dict:
    expect(f"mock price set to {value}", call("POST", f"/mock/products/{ASIN}", {"price": value})[0], 200)
    status, body = call("POST", f"/products/{product_id}/check")
    expect(f"manual check at {value}", (status, body["data"]["status"]), (200, "ok"))
    return body


def counts(product_id: int) -> tuple[int, int, int]:
    history = call("GET", f"/products/{product_id}/history")[1]["meta"]["total"]
    events = call("GET", f"/alert-events?product_id={product_id}")[1]["meta"]["total"]
    sent = [d for d in call("GET", "/notifications")[1]["data"] if d["status"] == "sent" and d["channel"] == "mock"]
    return history, events, len(sent)


def before_restart() -> None:
    expect("unauthenticated request is rejected", call("GET", "/products", token=None)[0], 401)
    expect("database initialised (empty product list)", call("GET", "/products")[1]["meta"]["total"], 0)
    status, body = call(
        "POST", "/products", {"input": f"https://www.amazon.com/dp/{ASIN}?tag=x", "discount_percent": "25"}
    )
    product = body["data"]
    expect(
        "product added through the mock provider",
        (status, product["baseline_price"], product["target_price"]),
        (201, "800.00", "600.00"),
    )
    pid = product["id"]
    expect("duplicate product rejected", call("POST", "/products", {"input": ASIN})[0], 409)
    # Checks here are seconds apart, not hours. Disable the default 60-minute cooldown for this run.
    rule_id = product["alerts"][0]["id"]
    expect("alert cooldown set to zero", call("PATCH", f"/alerts/{rule_id}", {"cooldown_minutes": 0})[0], 200)
    price(pid, "700.00")
    expect("price change recorded, no alert at $700", counts(pid), (2, 0, 0))
    price(pid, "599.00")
    expect("alert created and mock notification delivered at $599", counts(pid), (3, 1, 1))
    price(pid, "599.00")
    expect("no duplicate alert while the price stays at $599", counts(pid), (4, 1, 1))
    price(pid, "750.00")
    price(pid, "590.00")
    expect("new alert after recovery to $750 and drop to $590", counts(pid), (6, 2, 2))
    usage = call("GET", "/usage")[1]["data"]
    expect("API usage recorded", (usage["requests"], usage["paid_requests"], usage["cost_usd"]), (6, 0, "0.0000"))


def after_restart() -> None:
    products = call("GET", "/products")[1]["data"]
    expect(
        "product survived the restart",
        [(p["asin"], p["latest_price"], p["baseline_price"]) for p in products],
        [(ASIN, "590.00", "800.00")],
    )
    pid = products[0]["id"]
    expect("history, alerts and notifications survived the restart", counts(pid), (6, 2, 2))
    expect("alert state survived the restart", products[0]["alerts"][0]["is_below"], True)
    price(pid, "590.00")
    expect("no duplicate alert after the restart", counts(pid), (7, 2, 2))
    price(pid, "800.00")
    price(pid, "580.00")
    expect("alerts still work after the restart", counts(pid), (9, 3, 3))


if __name__ == "__main__":
    {"before": before_restart, "after": after_restart}[sys.argv[1]]()
