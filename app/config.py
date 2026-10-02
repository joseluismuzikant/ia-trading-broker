"""Typed application configuration loaded from environment variables.

Settings are read from the process environment and, when present, from a local
``.env`` file. Real secrets never live in Git; use ``.env.example`` as a guide.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Clearly insecure value used only so local development can start. Production
#: readiness checks reject it.
INSECURE_DEFAULT_SECRET = "change-me-insecure-development-secret"

#: Valid but public Fernet key. It keeps local development frictionless and is
#: rejected by the production readiness check. Replace it everywhere else.
INSECURE_DEFAULT_ENCRYPTION_KEY = "V19pwAKBBvM85X-KHh3zYNDq0l6LoROtAT8xGZ_2Jyg="


class Settings(BaseSettings):
    """Runtime settings for the application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -----------------------------------------------------
    app_name: str = "ia-trading-broker"
    environment: str = "development"

    # --- Security --------------------------------------------------------
    #: Reserved for future signing; not logged or exposed in diagnostics.
    secret_key: SecretStr = Field(default=INSECURE_DEFAULT_SECRET, min_length=16)
    session_cookie_name: str = "iatb_session"
    session_ttl_hours: int = Field(default=72, ge=1, le=24 * 30)
    cookie_secure: bool = False

    #: Fernet key that protects saved broker credentials. Generate one with:
    #: python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    credential_encryption_key: SecretStr = Field(
        default=INSECURE_DEFAULT_ENCRYPTION_KEY, min_length=32
    )

    # --- Database --------------------------------------------------------
    database_url: SecretStr = "postgresql+asyncpg://trading:trading@db:5432/trading"

    # --- IOL broker ------------------------------------------------------
    iol_base_url: str = "https://api.invertironline.com"
    iol_request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    #: IOL bills beyond the free monthly quota, so reads are capped by default.
    #: A positive value refuses further IOL calls once it is reached; 0 disables
    #: the cap (and accepts the cost). Every HTTP attempt counts as one call.
    iol_monthly_call_limit: int = Field(default=25_000, ge=0)
    #: Fraction of the limit at which a single warning is logged.
    iol_call_warn_ratio: float = Field(default=0.8, gt=0, le=1)

    # --- Trading universe ------------------------------------------------
    #: Where the universe and portfolio limits are declared. Empty means the
    #: file shipped with the application (``config/universe.toml``).
    universe_config_path: str = ""

    # --- OpenAI analysis (optional) --------------------------------------
    #: The portfolio analyst is not wired in yet, so no key is required to
    #: start: the deterministic advisor keeps working without one. SecretStr
    #: keeps the key masked in reprs, logs, and diagnostics.
    openai_api_key: SecretStr | None = None
    #: Model the analyst asks for. Nothing in the application is written
    #: against one particular model; this is only what the setting says.
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- Trading safety switches (both stay off until proven safe) --------
    live_trading_enabled: bool = False
    automatic_live_trading_enabled: bool = False

    @property
    def uses_insecure_encryption_key(self) -> bool:
        """True when saved credentials are protected by the public default key.

        This is not a readiness problem: a fresh development checkout must be
        able to start. It is still a real hazard, because anyone with the
        repository can decrypt every saved IOL password, so the application
        warns about it on startup.
        """
        return (
            self.credential_encryption_key.get_secret_value()
            == INSECURE_DEFAULT_ENCRYPTION_KEY
        )

    @property
    def is_production(self) -> bool:
        """True when running in a production-like environment."""
        return self.environment.strip().lower() in {"production", "prod"}

    def configuration_problems(self) -> list[str]:
        """Return a list of configuration problems found at runtime.

        An empty list means the configuration is considered acceptable.
        """
        problems: list[str] = []

        if len(self.secret_key.get_secret_value()) < 16:
            problems.append("secret_key must be at least 16 characters long")

        if len(self.credential_encryption_key.get_secret_value()) < 32:
            problems.append(
                "credential_encryption_key must be a valid Fernet key (44 characters)"
            )

        if self.is_production:
            if self.secret_key.get_secret_value() == INSECURE_DEFAULT_SECRET:
                problems.append("secret_key must be replaced before production use")
            if (
                self.credential_encryption_key.get_secret_value()
                == INSECURE_DEFAULT_ENCRYPTION_KEY
            ):
                problems.append(
                    "credential_encryption_key must be replaced before production use"
                )
            if not self.cookie_secure:
                problems.append("cookie_secure must be True in production")

        if not self.iol_base_url.startswith("https://") and self.is_production:
            problems.append("iol_base_url must use HTTPS in production")

        if self.live_trading_enabled and self.automatic_live_trading_enabled:
            problems.append(
                "automatic_live_trading_enabled must stay off until order handling is proven"
            )

        if not self.database_url.get_secret_value():
            problems.append("database_url must be set")

        return problems


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
