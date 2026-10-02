"""Tests for the configuration checks used by the readiness probe."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.config import (
    INSECURE_DEFAULT_ENCRYPTION_KEY,
    INSECURE_DEFAULT_SECRET,
    Settings,
)


def test_development_settings_are_acceptable() -> None:
    settings = Settings(
        environment="development",
        secret_key="a-development-secret-of-sufficient-length",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    assert settings.configuration_problems() == []


def test_production_rejects_default_secret() -> None:
    settings = Settings(
        environment="production",
        secret_key=INSECURE_DEFAULT_SECRET,
        cookie_secure=True,
        database_url="postgresql+asyncpg://u:p@db:5432/db",
    )
    problems = settings.configuration_problems()
    assert any("secret_key" in problem for problem in problems)


def test_production_requires_secure_cookie() -> None:
    settings = Settings(
        environment="production",
        secret_key="a-real-production-secret-value",
        cookie_secure=False,
        database_url="postgresql+asyncpg://u:p@db:5432/db",
    )
    problems = settings.configuration_problems()
    assert any("cookie_secure" in problem for problem in problems)


def test_automatic_live_trading_flagged() -> None:
    settings = Settings(
        environment="development",
        secret_key="a-development-secret-of-sufficient-length",
        database_url="sqlite+aiosqlite:///:memory:",
        live_trading_enabled=True,
        automatic_live_trading_enabled=True,
    )
    problems = settings.configuration_problems()
    assert any("automatic_live_trading_enabled" in problem for problem in problems)


def test_trading_switches_default_to_off() -> None:
    settings = Settings(
        environment="development",
        secret_key="a-development-secret-of-sufficient-length",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    assert settings.live_trading_enabled is False
    assert settings.automatic_live_trading_enabled is False


def test_public_encryption_key_is_detected_but_not_a_readiness_problem() -> None:
    # A fresh development checkout must still start, so the public default key
    # is a warning rather than a readiness failure.
    settings = Settings(
        environment="development",
        secret_key="a-development-secret-of-sufficient-length",
        credential_encryption_key=INSECURE_DEFAULT_ENCRYPTION_KEY,
        database_url="sqlite+aiosqlite:///:memory:",
    )

    assert settings.uses_insecure_encryption_key is True
    assert settings.configuration_problems() == []


def test_a_private_encryption_key_is_not_flagged() -> None:
    settings = Settings(
        environment="development",
        secret_key="a-development-secret-of-sufficient-length",
        credential_encryption_key="yVzT1uXn2Q0mA5c9rLbPkHoJ8dEgS6wFzR3tIq4vM7s=",
        database_url="sqlite+aiosqlite:///:memory:",
    )

    assert settings.uses_insecure_encryption_key is False
    assert settings.configuration_problems() == []


def test_production_rejects_the_public_encryption_key() -> None:
    settings = Settings(
        environment="production",
        secret_key="a-real-production-secret-value",
        cookie_secure=True,
        credential_encryption_key=INSECURE_DEFAULT_ENCRYPTION_KEY,
        database_url="postgresql+asyncpg://u:p@db:5432/db",
    )

    problems = settings.configuration_problems()
    assert any("credential_encryption_key" in problem for problem in problems)


# --- OpenAI settings ------------------------------------------------------


def test_the_application_starts_without_an_openai_key() -> None:
    # The analyst is not wired in yet, so a missing key must never stop the
    # application or the tests.
    settings = Settings(
        _env_file=None,
        environment="development",
        secret_key="a-development-secret-of-sufficient-length",
        database_url="sqlite+aiosqlite:///:memory:",
    )

    assert settings.openai_api_key is None
    assert settings.configuration_problems() == []


def test_the_openai_key_is_kept_secret() -> None:
    settings = Settings(
        _env_file=None,
        environment="development",
        secret_key="a-development-secret-of-sufficient-length",
        database_url="sqlite+aiosqlite:///:memory:",
        openai_api_key="sk-test-not-a-real-key",
    )

    assert isinstance(settings.openai_api_key, SecretStr)
    assert "sk-test-not-a-real-key" not in repr(settings)
    assert "sk-test-not-a-real-key" not in str(settings.openai_api_key)
    assert settings.openai_api_key.get_secret_value() == "sk-test-not-a-real-key"


def test_an_invalid_openai_timeout_is_rejected() -> None:
    from pydantic import ValidationError

    for timeout in (0, -1):
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                secret_key="a-development-secret-of-sufficient-length",
                database_url="sqlite+aiosqlite:///:memory:",
                openai_timeout_seconds=timeout,
            )
