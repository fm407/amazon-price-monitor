"""Apify adapter.

Verified against https://docs.apify.com/api/v2:
- POST /v2/actors/{actorId}/runs starts a run and returns 201 with `data.id`, `data.status`,
  `data.defaultDatasetId` and `data.usageTotalUsd`. Query parameters used here: `waitForFinish`
  (maximum 60 seconds), `timeout` (run limit in seconds) and `maxTotalChargeUsd` (cost cap).
- GET /v2/actor-runs/{runId}?waitForFinish=60 polls a run.
- Run statuses: READY, RUNNING, SUCCEEDED, FAILED, TIMING-OUT, TIMED-OUT, ABORTING, ABORTED.
- GET /v2/datasets/{datasetId}/items returns the result items.
- Authentication: `Authorization: Bearer <token>`.

No Actor is built in. The Actor ID, its input and the mapping from its output fields are
configuration, because every Actor defines its own schemas and pricing.
"""

import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx

from app.amazon import MARKETPLACES, canonical_url
from app.money import InvalidAmount, parse_amount
from app.providers.base import (
    AUTH_BLOCK_SECONDS,
    ProductData,
    ProviderError,
    parse_retry_after,
    resolve_currency,
)

WAIT_SECONDS = 60
IN_PROGRESS = {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}
POLL_ERROR_PAUSE_SECONDS = 5.0
# Output paths of the public junglee/amazon-crawler Actor, as shown on its store page on 2026-11-18.
DEFAULT_FIELD_MAP = {
    "title": "title",
    "brand": "brand",
    "image_url": "thumbnailImage",
    "price": "price.value",
    "currency": "price.currency",
    "list_price": "listPrice.value",
    "shipping": "shippingPrice.value",
    "seller": "seller.name",
    "in_stock": "inStock",
    "condition": "",
}


class ApifyConfigError(ValueError):
    pass


def parse_json_object(text: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise ApifyConfigError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ApifyConfigError(f"{name} must be a JSON object")
    return value


def render_input(template: Any, values: dict[str, str]) -> Any:
    """Replace {url}, {asin}, {marketplace} and {domain} placeholders inside every string of the template."""
    if isinstance(template, str):
        for key, value in values.items():
            template = template.replace("{" + key + "}", value)
        return template
    if isinstance(template, list):
        return [render_input(item, values) for item in template]
    if isinstance(template, dict):
        return {key: render_input(item, values) for key, item in template.items()}
    return template


def dig(item: Any, path: str) -> Any:
    """Follow a dotted path such as `price.value`. Missing parts yield None."""
    if not path:
        return None
    for part in path.split("."):
        if isinstance(item, dict):
            item = item.get(part)
        elif isinstance(item, list) and part.isdigit() and int(part) < len(item):
            item = item[int(part)]
        else:
            return None
    return item


def _cost(run: dict[str, Any]) -> Decimal | None:
    try:
        return parse_amount(run.get("usageTotalUsd"))
    except InvalidAmount:
        return None


class ApifyProvider:
    name = "apify"
    is_paid = True
    max_monthly_requests: int | None = None
    max_monthly_spend_usd: Decimal | None = None

    def __init__(
        self,
        client: httpx.Client,
        *,
        base_url: str,
        token: str,
        actor_id: str,
        input_template: str,
        field_map: str,
        cost_per_request_usd: Decimal,
        max_concurrency: int,
        run_timeout_seconds: int,
        max_charge_per_run_usd: Decimal | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/") + "/v2"
        self._token = token
        self._actor_id = actor_id.strip().replace("/", "~")
        self._input_template = parse_json_object(input_template, "APIFY_INPUT_TEMPLATE")
        self._field_map = {**DEFAULT_FIELD_MAP, **parse_json_object(field_map, "APIFY_FIELD_MAP")}
        self.cost_per_request_usd = cost_per_request_usd
        self.max_concurrency = max_concurrency
        self._run_timeout = run_timeout_seconds
        self._max_charge = max_charge_per_run_usd
        self._sleep = sleep
        self._clock = clock

    @property
    def configured(self) -> bool:
        return bool(self._token and self._actor_id)

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        try:
            response = self._client.request(
                method, url, headers={"Authorization": f"Bearer {self._token}"}, timeout=WAIT_SECONDS + 30, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "Apify request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network", f"Apify network error: {type(exc).__name__}") from exc
        status = response.status_code
        if status in (401, 403):
            raise ProviderError(
                "auth", "Apify rejected the API token", http_status=status, block_seconds=AUTH_BLOCK_SECONDS
            )
        if status == 402:
            raise ProviderError(
                "quota", "Apify account limit or payment required", http_status=status, block_seconds=AUTH_BLOCK_SECONDS
            )
        if status == 404:
            raise ProviderError("invalid_request", "Apify Actor, run or dataset not found", http_status=status)
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            raise ProviderError("rate_limited", "Apify rate limit reached", http_status=status, retry_after=retry_after)
        if status >= 400:
            raise ProviderError("http_error", f"Apify returned HTTP {status}", http_status=status)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("malformed", "Apify returned a non-JSON response", http_status=status) from exc

    @staticmethod
    def _run_of(body: Any) -> dict[str, Any]:
        run = body.get("data") if isinstance(body, dict) else None
        if not isinstance(run, dict) or not run.get("id") or not run.get("status"):
            raise ProviderError("malformed", "Apify returned an unexpected run object", http_status=200)
        return run

    def _start_run(self, asin: str, marketplace: str) -> dict[str, Any]:
        values = {
            "url": canonical_url(asin, marketplace),
            "asin": asin,
            "marketplace": marketplace,
            "domain": MARKETPLACES[marketplace].domain,
        }
        params: dict[str, Any] = {"waitForFinish": WAIT_SECONDS, "timeout": self._run_timeout}
        if self._max_charge is not None:
            params["maxTotalChargeUsd"] = str(self._max_charge)
        url = f"{self._base}/actors/{self._actor_id}/runs"
        try:
            body = self._request("POST", url, params=params, json=render_input(self._input_template, values))
        except ProviderError as err:
            # Only a rate-limited start is retried: it is the one failure where no run can have started.
            err.retryable = err.category == "rate_limited"
            raise
        return self._run_of(body)

    def _wait(self, run: dict[str, Any]) -> dict[str, Any]:
        """Poll until the run ends. Polling is free, so transient polling errors are tolerated."""
        deadline = self._clock() + self._run_timeout + WAIT_SECONDS
        while run["status"] in IN_PROGRESS:
            if self._clock() >= deadline:
                raise ProviderError("timeout", "Apify run did not finish in time", actual_cost_usd=_cost(run))
            try:
                run = self._run_of(
                    self._request("GET", f"{self._base}/actor-runs/{run['id']}", params={"waitForFinish": WAIT_SECONDS})
                )
            except ProviderError as err:
                if err.category not in ("timeout", "network", "http_error", "rate_limited"):
                    raise
                self._sleep(POLL_ERROR_PAUSE_SECONDS)
        return run

    def fetch_product(self, asin: str, marketplace: str) -> ProductData:
        if not self.configured:
            raise ProviderError("not_configured", "APIFY_TOKEN or APIFY_ACTOR_ID is not set")
        if marketplace not in MARKETPLACES:
            raise ProviderError("unsupported_marketplace", f"Unknown marketplace {marketplace}")
        run = self._wait(self._start_run(asin, marketplace))
        cost = _cost(run)
        if run["status"] != "SUCCEEDED":
            detail = str(run.get("statusMessage") or "")[:120]
            raise ProviderError(
                "run_failed", f"Apify run ended as {run['status']}. {detail}".strip(), actual_cost_usd=cost
            )
        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            raise ProviderError("malformed", "Apify run has no default dataset", actual_cost_usd=cost)
        try:
            items = self._request(
                "GET",
                f"{self._base}/datasets/{dataset_id}/items",
                params={"format": "json", "clean": "true", "limit": 5},
            )
        except ProviderError as err:
            err.actual_cost_usd = cost
            raise
        if not isinstance(items, list):
            raise ProviderError("malformed", "Apify dataset is not a list", http_status=200, actual_cost_usd=cost)
        products = [item for item in items if isinstance(item, dict) and not item.get("error")]
        if not products:
            raise ProviderError(
                "not_found", "Apify returned no product for this ASIN", http_status=200, actual_cost_usd=cost
            )
        return self._to_product_data(asin, marketplace, products[0], cost)

    def _to_product_data(self, asin: str, marketplace: str, item: dict[str, Any], cost: Decimal | None) -> ProductData:
        def field(name: str) -> Any:
            return dig(item, self._field_map.get(name, ""))

        def text(name: str) -> str | None:
            value = field(name)
            return value.strip() if isinstance(value, str) and value.strip() else None

        in_stock = field("in_stock")
        condition = (text("condition") or "").lower()
        return ProductData(
            asin=asin,
            marketplace=marketplace,
            title=text("title"),
            brand=text("brand"),
            image_url=text("image_url"),
            currency=resolve_currency(text("currency"), marketplace),
            price=field("price"),
            list_price=field("list_price"),
            shipping=field("shipping"),  # None stays unknown; never assumed free
            seller=text("seller"),
            condition="new" if condition == "new" else ("used" if condition else "unknown"),
            availability="unknown" if not isinstance(in_stock, bool) else ("in_stock" if in_stock else "out_of_stock"),
            price_basis="actor_item_price",
            raw={name: field(name) for name in ("price", "currency", "list_price", "shipping", "seller", "in_stock")},
            actual_cost_usd=cost,
        )
