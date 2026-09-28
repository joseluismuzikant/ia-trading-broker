"""Shared pytest fixtures.

The environment is configured before the application modules are imported so
that settings point at an isolated SQLite database instead of PostgreSQL.
"""

from __future__ import annotations

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import User
from app.services import auth as auth_service

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

DEFAULT_PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    """An in-memory SQLite engine shared by every connection in a test."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: Any) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the test engine."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A database session for service-level tests."""
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def make_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[..., Any]:
    """Return a factory that creates users in the test database."""

    async def _make_user(
        username: str = "alice",
        password: str = DEFAULT_PASSWORD,
        *,
        email: str | None = None,
        is_admin: bool = False,
        is_active: bool = True,
    ) -> User:
        async with session_factory() as session:
            return await auth_service.create_user(
                session,
                username=username,
                password=password,
                email=email,
                is_admin=is_admin,
                is_active=is_active,
            )

    return _make_user


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired to the app with the test database."""

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


@pytest.fixture
def password() -> str:
    """The password used by the default test users."""
    return DEFAULT_PASSWORD
