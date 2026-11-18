"""Environment-based configuration. Secrets may also come from /run/secrets files."""

from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = Path("/run/secrets")
PAID_PROVIDERS = ("canopy", "apify", "serpapi", "easyparser", "scrapingdog")
PROVIDER_NAMES = ("mock", *PAID_PROVIDERS)
MIN_SECRET_LENGTH = 32
DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        secrets_dir=str(SECRETS_DIR) if SECRETS_DIR.is_dir() else None,
    )

    # Application
    log_level: str = "INFO"
    data_dir: Path = Path("data")
    database_url: str = ""
    timezone: str = "America/New_York"
    secret_key: str = ""  # generated and persisted in data_dir when empty
    api_token: str = ""  # optional bearer token for REST clients
    session_cookie_secure: bool = False
    session_max_age_hours: int = 168

    # Providers
    provider: str = "mock"
    fallback_provider: str = ""
    demo_mode: bool = False
    http_timeout_seconds: float = 20.0
    provider_max_attempts: int = 3

    canopy_api_key: str = ""
    canopy_base_url: str = "https://rest.canopyapi.co"
    canopy_cost_per_request_usd: Decimal = Decimal("0.01")
    canopy_max_concurrency: int = 2
    canopy_max_monthly_requests: int | None = Field(default=None, ge=0)  # e.g. 100 for a free plan
    canopy_max_monthly_spend_usd: Decimal | None = Field(default=None, ge=0)

    apify_token: str = ""
    apify_actor_id: str = ""
    apify_base_url: str = "https://api.apify.com"
    apify_input_template: str = '{"categoryOrProductUrls": [{"url": "{url}"}], "maxItemsPerStartUrl": 1}'
    apify_field_map: str = ""  # JSON object overriding the default output mapping
    apify_cost_per_run_usd: Decimal = Decimal("0.01")
    apify_max_charge_per_run_usd: Decimal | None = None
    apify_run_timeout_seconds: int = 180
    apify_max_concurrency: int = 1
    apify_max_monthly_requests: int | None = Field(default=None, ge=0)
    apify_max_monthly_spend_usd: Decimal | None = Field(default=None, ge=0)  # e.g. 5.00 for the free plan credit

    # SerpApi
    serpapi_api_key: str = ""
    serpapi_cost_per_request_usd: Decimal = Decimal("0.01")
    serpapi_max_monthly_requests: int | None = Field(default=None, ge=0)
    serpapi_max_monthly_spend_usd: Decimal | None = Field(default=None, ge=0)

    # Easyparser
    easyparser_api_key: str = ""
    easyparser_cost_per_request_usd: Decimal = Decimal("0.01")
    easyparser_max_monthly_requests: int | None = Field(default=None, ge=0)
    easyparser_max_monthly_spend_usd: Decimal | None = Field(default=None, ge=0)

    # Scrapingdog
    scrapingdog_api_key: str = ""
    scrapingdog_cost_per_request_usd: Decimal = Decimal("0.01")
    scrapingdog_max_monthly_requests: int | None = Field(default=None, ge=0)
    scrapingdog_max_monthly_spend_usd: Decimal | None = Field(default=None, ge=0)

    # Cost controls
    allow_paid_api_requests: bool = False
    max_monthly_api_spend_usd: Decimal = Decimal("5.00")
    max_daily_api_requests: int = 100
    default_check_interval_minutes: int = 360
    pace_monthly_limits: bool = True  # spread each provider's monthly limit over the month
    confirm_drop_percent: Decimal = Field(default=Decimal(0), ge=0, lt=100)  # 0 = off
    confirm_recheck_minutes: int = Field(default=15, ge=1, le=1440)

    # Scheduler
    scheduler_enabled: bool = True
    scheduler_tick_seconds: float = 30.0
    scheduler_batch_size: int = 5

    # Backups
    backup_interval_hours: int = Field(default=24, ge=0)  # 0 turns scheduled backups off
    backup_keep: int = Field(default=7, ge=1)

    # Notifications
    discord_enabled: bool = True
    discord_webhook_url: str = ""  # secret: the URL contains the webhook token
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"  # starttls | ssl | none
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    notify_mock_enabled: bool | None = None  # default: on when PROVIDER=mock
    alert_chart_enabled: bool = True  # attach a price history chart to alert messages
    alert_chart_days: int = Field(default=30, ge=1, le=3650)

    @field_validator(
        "apify_max_charge_per_run_usd",
        "notify_mock_enabled",
        "canopy_max_monthly_requests",
        "canopy_max_monthly_spend_usd",
        "apify_max_monthly_requests",
        "apify_max_monthly_spend_usd",
        "serpapi_max_monthly_requests",
        "serpapi_max_monthly_spend_usd",
        "easyparser_max_monthly_requests",
        "easyparser_max_monthly_spend_usd",
        "scrapingdog_max_monthly_requests",
        "scrapingdog_max_monthly_spend_usd",
        mode="before",
    )
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """`KEY=` in .env means "not set" for optional values."""
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.provider not in PROVIDER_NAMES:
            raise ValueError(f"PROVIDER must be one of {PROVIDER_NAMES}")
        fallbacks = self.fallback_providers
        if fallbacks:
            if self.provider == "mock":
                raise ValueError("FALLBACK_PROVIDER cannot be used with PROVIDER=mock")
            for name in fallbacks:
                # A mock fallback would mix fictional prices into real history.
                if name not in PAID_PROVIDERS:
                    raise ValueError(f"FALLBACK_PROVIDER entries must be one of {PAID_PROVIDERS}")
            if self.provider in fallbacks or len(set(fallbacks)) != len(fallbacks):
                raise ValueError("FALLBACK_PROVIDER must not repeat a provider or contain PROVIDER itself")
        for name in ("secret_key", "api_token"):
            value = getattr(self, name)
            if value and len(value) < MIN_SECRET_LENGTH:
                raise ValueError(
                    f"{name.upper()} must be at least {MIN_SECRET_LENGTH} characters. Try: openssl rand -hex 32"
                )
        if self.demo_mode and self.provider != "mock":
            raise ValueError("DEMO_MODE=true requires PROVIDER=mock")
        if self.discord_webhook_url:
            parts = urlsplit(self.discord_webhook_url)
            if (
                parts.scheme != "https"
                or parts.hostname not in DISCORD_HOSTS
                or not parts.path.startswith("/api/webhooks/")
            ):
                raise ValueError("DISCORD_WEBHOOK_URL must look like https://discord.com/api/webhooks/<id>/<token>")
        if self.smtp_security not in ("starttls", "ssl", "none"):
            raise ValueError("SMTP_SECURITY must be starttls, ssl or none")
        if self.default_check_interval_minutes < 5:
            raise ValueError("DEFAULT_CHECK_INTERVAL_MINUTES must be at least 5")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown TIMEZONE: {self.timezone}") from exc
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.data_dir / 'monitor.db').resolve()}"
        return self

    @property
    def fallback_providers(self) -> list[str]:
        """FALLBACK_PROVIDER is one name or a comma-separated list, tried in order."""
        return [name.strip().lower() for name in self.fallback_provider.split(",") if name.strip()]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def mock_notifier_enabled(self) -> bool:
        return self.provider == "mock" if self.notify_mock_enabled is None else self.notify_mock_enabled

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_enabled and self.discord_webhook_url)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_to and self.smtp_from)

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs or stored error text."""
        candidates = [
            self.secret_key,
            self.api_token,
            self.canopy_api_key,
            self.apify_token,
            self.serpapi_api_key,
            self.easyparser_api_key,
            self.scrapingdog_api_key,
            self.discord_webhook_url,
            self.discord_webhook_url.rstrip("/").rsplit("/", 1)[-1],  # the token part alone
            self.smtp_password,
        ]
        return [value for value in candidates if len(value) >= 6]
