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

    # --- PPI broker ------------------------------------------------------
    #: PPI's REST API. The sandbox is the default on purpose: production needs
    #: an explicit decision. ``AuthorizedClient`` and ``ClientKey`` are issued
    #: by PPI to the integration; each user's own public and private API keys
    #: are stored with their connection, never here.
    ppi_base_url: str = "https://clientapi_sandbox.portfoliopersonal.com"
    ppi_request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    ppi_authorized_client: SecretStr | None = None
    ppi_client_key: SecretStr | None = None

    # --- Approvals ---------------------------------------------------------
    #: How long an approved plan may still be executed. The plan's prices come
    #: from the moment it was made, so after this window nothing is sent from
    #: it and a new analysis is needed.
    approval_execution_window_hours: int = Field(default=24, ge=1, le=24 * 7)

    # --- Analysis ------------------------------------------------------------
    #: How many times one run may ask the model. A refused or unfillable answer
    #: is sent back once with the reasons; this bounds that loop. 1 disables it.
    analysis_max_attempts: int = Field(default=2, ge=1, le=3)

    # --- Trading universe ------------------------------------------------
    #: Where the universe and portfolio limits are declared. Empty means the
    #: file shipped with the application (``config/universe.toml``).
    universe_config_path: str = ""

    # --- OpenAI analysis (optional) --------------------------------------
    #: Provider keys. A model is picked per analysis from
    #: ``config/models.toml``, which names the key it needs; none of them is
    #: required to start, and SecretStr keeps them masked in reprs, logs, and
    #: diagnostics.
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    mimo_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    xai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    #: Seconds to wait for one model call, whichever provider answers.
    openai_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- Analysis models -------------------------------------------------
    #: Where the models a run may ask are declared. Empty means the file
    #: shipped with the application (``config/models.toml``).
    models_config_path: str = ""
    #: Catalog key an analysis preselects. Empty means the catalog's own
    #: default, so a deployment can point every user at one model.
    analysis_model_default: str = ""

    # --- Live IOL execution parameters (fail-closed) ----------------------
    #: Settlement term IOL requires on every trading order (e.g. "t2"). There
    #: is no safe default: live execution refuses to send anything while this
    #: is empty, rather than inventing a trading parameter.
    iol_live_plazo: str = ""
    #: How many days an order stays valid, sent to IOL as its ``validez`` date.
    #: 0 means "not configured" and live execution fails closed.
    iol_live_validity_days: int = Field(default=0, ge=0)

    # --- Live PPI execution parameters (fail-closed) -----------------------
    #: Settlement PPI requires on every order (e.g. "INMEDIATA" or "A-48HS").
    #: Empty means live PPI execution refuses to send anything.
    ppi_live_settlement: str = ""
    #: How long an order lives (e.g. "HASTA-SU-EJECUCIÓN"). Empty fails closed.
    ppi_live_operation_term: str = ""

    # --- LangSmith tracing (optional; off and keyless by default) ---------
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "ia-trading-broker"
    langsmith_endpoint: str = ""
    langsmith_workspace_id: str = ""

    @property
    def uses_insecure_encryption_key(self) -> bool:
        """True when saved credentials are protected by the public default key.

        This is not a readiness problem: a fresh development checkout must be
        able to start. It is still a real hazard, because anyone with the
        repository can decrypt every saved IOL password, so the application
        warns about it on startup.
        """
        return self.credential_encryption_key.get_secret_value() == INSECURE_DEFAULT_ENCRYPTION_KEY

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
            problems.append("credential_encryption_key must be a valid Fernet key (44 characters)")

        if self.is_production:
            if self.secret_key.get_secret_value() == INSECURE_DEFAULT_SECRET:
                problems.append("secret_key must be replaced before production use")
            if self.credential_encryption_key.get_secret_value() == INSECURE_DEFAULT_ENCRYPTION_KEY:
                problems.append("credential_encryption_key must be replaced before production use")
            if not self.cookie_secure:
                problems.append("cookie_secure must be True in production")

        if not self.iol_base_url.startswith("https://") and self.is_production:
            problems.append("iol_base_url must use HTTPS in production")

        if not self.database_url.get_secret_value():
            problems.append("database_url must be set")

        return problems


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
