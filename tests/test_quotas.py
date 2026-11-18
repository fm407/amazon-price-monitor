"""Per-provider monthly limits: use one provider's free allowance, then move on to the next."""

from decimal import Decimal

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import budget
from app.checker import check_product
from app.main import create_app
from app.models import PriceObservation, ProviderRequest
from tests.test_apify import USD_ITEM, paid_ctx, run, tracked
from tests.test_providers import CANOPY_OK


def both_ok(calls: list[str]):
    """Canopy and Apify both answer successfully. Apify reports a cost of $0.40 per run."""

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "rest.canopyapi.co":
            calls.append("canopy")
            return httpx.Response(200, json=CANOPY_OK)
        if path.endswith("/runs"):
            calls.append("apify")
            return httpx.Response(201, json=run("SUCCEEDED", 0.40))
        return httpx.Response(200, json=[USD_ITEM])

    return handler


def categories(ctx) -> list[tuple[str, str]]:
    with ctx.sessions() as session:
        rows = session.scalars(select(ProviderRequest).order_by(ProviderRequest.id))
        return [(row.provider, row.category) for row in rows]


def test_request_quota_spills_over_to_the_next_provider(tmp_path):
    calls: list[str] = []
    ctx = paid_ctx(tmp_path, both_ok(calls), canopy_max_monthly_requests=2, max_monthly_api_spend_usd=Decimal("50"))
    pid = tracked(ctx)
    providers = [check_product(ctx, pid).provider for _ in range(4)]
    assert providers == ["canopy", "canopy", "apify", "apify"]
    assert calls == ["canopy", "canopy", "apify", "apify"]
    assert categories(ctx)[2:4] == [("canopy", "skipped_provider_quota"), ("apify", "ok")]
    with ctx.sessions() as session:
        assert [o.provider for o in session.scalars(select(PriceObservation))] == ["canopy", "canopy", "apify", "apify"]
        assert budget.provider_month(session, "canopy", ctx.now())[0] == 2  # skips do not count as requests


def test_both_allowances_used_up_stops_checks_until_next_month(tmp_path):
    calls: list[str] = []
    ctx = paid_ctx(
        tmp_path, both_ok(calls), canopy_max_monthly_requests=1, apify_max_monthly_spend_usd=Decimal("1.00"),
        apify_cost_per_run_usd=Decimal("0.40"), max_monthly_api_spend_usd=Decimal("50"),
    )  # fmt: skip
    pid = tracked(ctx)
    assert [check_product(ctx, pid).provider for _ in range(3)] == ["canopy", "apify", "apify"]
    result = check_product(ctx, pid)  # Apify: $0.80 used, another $0.40 would exceed its $1.00 allowance
    assert (result.status, result.category) == ("skipped", "provider_quota")
    assert calls == ["canopy", "apify", "apify"]
    assert categories(ctx)[-2:] == [("canopy", "skipped_provider_quota"), ("apify", "skipped_provider_quota")]
    ctx.now.advance(days=20)  # next calendar month: both allowances are fresh
    assert check_product(ctx, pid).provider == "canopy"


def test_quotas_survive_a_restart(tmp_path):
    calls: list[str] = []
    ctx = paid_ctx(tmp_path, both_ok(calls), canopy_max_monthly_requests=1, max_monthly_api_spend_usd=Decimal("50"))
    pid = tracked(ctx)
    assert check_product(ctx, pid).provider == "canopy"
    ctx.close()
    restarted = paid_ctx(
        tmp_path, both_ok(calls), canopy_max_monthly_requests=1, max_monthly_api_spend_usd=Decimal("50")
    )
    assert check_product(restarted, pid).provider == "apify"


def test_global_budget_still_stops_everything(tmp_path):
    calls: list[str] = []
    ctx = paid_ctx(
        tmp_path, both_ok(calls), canopy_max_monthly_requests=1, canopy_cost_per_request_usd=Decimal("0"),
        max_monthly_api_spend_usd=Decimal("0.01"),
    )  # fmt: skip
    pid = tracked(ctx)
    assert check_product(ctx, pid).provider == "canopy"  # free: costs nothing against the global budget
    result = check_product(ctx, pid)  # Canopy quota reached, and an Apify run would exceed the global $0.01
    assert (result.status, result.category) == ("skipped", "monthly_budget")
    assert calls == ["canopy"]


def test_no_limits_means_unchanged_behaviour(tmp_path):
    calls: list[str] = []
    ctx = paid_ctx(tmp_path, both_ok(calls), max_monthly_api_spend_usd=Decimal("50"))
    pid = tracked(ctx)
    assert [check_product(ctx, pid).provider for _ in range(3)] == ["canopy"] * 3


def test_settings_page_and_api_show_usage_per_provider(tmp_path):
    ctx = paid_ctx(
        tmp_path, both_ok([]), canopy_max_monthly_requests=100, apify_max_monthly_spend_usd=Decimal("5.00"),
        api_token="quota-display-token-0123456789abcdef", max_monthly_api_spend_usd=Decimal("50"),
    )  # fmt: skip
    check_product(ctx, tracked(ctx))
    with TestClient(create_app(ctx=ctx)) as client:
        rows = client.get(
            "/api/v1/providers", headers={"Authorization": "Bearer quota-display-token-0123456789abcdef"}
        ).json()["data"]
    by_name = {row["name"]: row for row in rows}
    assert (by_name["canopy"]["month_requests"], by_name["canopy"]["monthly_limit"]) == (1, "100 requests")
    assert (by_name["apify"]["month_requests"], by_name["apify"]["monthly_limit"]) == (0, "$5.00")


def test_unconfigured_provider_in_the_chain_reserves_nothing(tmp_path):
    calls: list[str] = []
    ctx = paid_ctx(tmp_path, both_ok(calls), canopy_api_key="", max_monthly_api_spend_usd=Decimal("50"))
    pid = tracked(ctx)
    assert check_product(ctx, pid).provider == "apify"  # Canopy has no key: skipped without a ledger entry
    assert categories(ctx) == [("apify", "ok")]
    with ctx.sessions() as session:
        assert budget.provider_month(session, "canopy", ctx.now()) == (0, 0)


def test_negative_limits_are_rejected():
    import pytest

    from app.config import Settings

    for field in ("canopy_max_monthly_requests", "apify_max_monthly_spend_usd"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **{field: -1})
