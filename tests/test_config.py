"""Tests for the configuration checks used by the readiness probe."""

from __future__ import annotations

from app.config import INSECURE_DEFAULT_SECRET, Settings


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
