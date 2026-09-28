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

    # --- Database --------------------------------------------------------
    database_url: SecretStr = "postgresql+asyncpg://trading:trading@db:5432/trading"

    # --- Trading safety switches (both stay off until proven safe) --------
    live_trading_enabled: bool = False
    automatic_live_trading_enabled: bool = False

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

        if self.is_production:
            if self.secret_key.get_secret_value() == INSECURE_DEFAULT_SECRET:
                problems.append("secret_key must be replaced before production use")
            if not self.cookie_secure:
                problems.append("cookie_secure must be True in production")

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
