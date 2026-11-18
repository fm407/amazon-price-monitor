"""Provider registry."""

import httpx

from app.config import Settings
from app.providers.base import PriceProvider, ProductData, ProviderError
from app.providers.canopy import CanopyProvider
from app.providers.mock import MockProvider

__all__ = ["PriceProvider", "ProductData", "ProviderError", "build_providers"]


def build_providers(settings: Settings, client: httpx.Client) -> dict[str, PriceProvider]:
    """The mock provider is registered only in mock mode, so fictional and real prices never mix."""
    if settings.provider == "mock":
        return {"mock": MockProvider()}
    return {
        "canopy": CanopyProvider(
            client,
            settings.canopy_base_url,
            settings.canopy_api_key,
            settings.canopy_cost_per_request_usd,
            settings.canopy_max_concurrency,
        ),
    }
