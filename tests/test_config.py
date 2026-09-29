"""Tests for the configuration checks used by the readiness probe."""

from __future__ import annotations

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
