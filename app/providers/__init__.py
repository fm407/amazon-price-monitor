"""Provider registry."""

import httpx

from app.config import Settings
from app.providers.apify import ApifyProvider
from app.providers.base import PriceProvider, ProductData, ProviderError
from app.providers.canopy import CanopyProvider
from app.providers.easyparser import EasyparserProvider
from app.providers.mock import MockProvider
from app.providers.scrapingdog import ScrapingdogProvider
from app.providers.serpapi import SerpApiProvider

__all__ = ["PriceProvider", "ProductData", "ProviderError", "build_providers"]


def build_providers(settings: Settings, client: httpx.Client) -> dict[str, PriceProvider]:
    """The mock provider is registered only in mock mode, so fictional and real prices never mix."""
    if settings.provider == "mock":
        return {"mock": MockProvider()}
    canopy = CanopyProvider(
        client,
        settings.canopy_base_url,
        settings.canopy_api_key,
        settings.canopy_cost_per_request_usd,
        settings.canopy_max_concurrency,
    )
    apify = ApifyProvider(
        client,
        base_url=settings.apify_base_url,
        token=settings.apify_token,
        actor_id=settings.apify_actor_id,
        input_template=settings.apify_input_template,
        field_map=settings.apify_field_map,
        cost_per_request_usd=settings.apify_cost_per_run_usd,
        max_concurrency=settings.apify_max_concurrency,
        run_timeout_seconds=settings.apify_run_timeout_seconds,
        max_charge_per_run_usd=settings.apify_max_charge_per_run_usd,
    )
    providers: dict[str, PriceProvider] = {"canopy": canopy, "apify": apify}
    for simple in (SerpApiProvider, EasyparserProvider, ScrapingdogProvider):
        key = getattr(settings, f"{simple.name}_api_key")
        providers[simple.name] = simple(client, key, getattr(settings, f"{simple.name}_cost_per_request_usd"))
    for name, provider in providers.items():
        provider.max_monthly_requests = getattr(settings, f"{name}_max_monthly_requests")
        provider.max_monthly_spend_usd = getattr(settings, f"{name}_max_monthly_spend_usd")
    return providers
