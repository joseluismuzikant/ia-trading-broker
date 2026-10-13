"""Tests for the monthly IOL API call budget.

IOL is free up to a monthly quota and bills for every call past it, so the
adapter counts calls and must stop *before* sending one that would cost money.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.config import get_settings
from app.infrastructure.iol import call_budget
from app.infrastructure.iol.client import IOLClient
from app.infrastructure.iol.errors import IOLBudgetExceededError, IOLResponseError
from tests.fakes import FakeIOL

USERNAME = "iol-test-user"
PASSWORD = "iol-test-password"


def build_client(fake: FakeIOL) -> IOLClient:
    """An IOL client wired to the fake transport."""
    return IOLClient(base_url="http://iol.test", timeout=5.0, transport=fake.transport)


def set_limit(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    """Override the call limit on the cached settings object."""
    monkeypatch.setattr(get_settings(), "iol_monthly_call_limit", limit)


async def test_usage_starts_at_zero() -> None:
    usage = call_budget.usage

    assert usage.used == 0
    assert usage.limit == 25_000
    assert usage.remaining == 25_000
    assert usage.ratio == 0.0


async def test_every_call_is_counted() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        await client.get_profile(token.access_token)

    # One token call plus one read.
    assert call_budget.usage.used == 2


async def test_each_retry_attempt_is_counted() -> None:
    fake = FakeIOL(profile_error_status=500)

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLResponseError):
            await client.get_profile(token.access_token)

    # The read is retried twice, and every attempt is one billable call.
    assert len(fake.authenticated_paths) == 3
    assert call_budget.usage.used == 4


async def test_the_cap_refuses_the_call_before_sending_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_limit(monkeypatch, 1)
    fake = FakeIOL()

    async with build_client(fake) as client:
        await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLBudgetExceededError):
            await client.get_profile("a-token")

    # Nothing was sent, so nothing was billed.
    assert fake.authenticated_paths == []
    assert call_budget.usage.used == 1


async def test_reads_below_the_cap_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    set_limit(monkeypatch, 2)
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        profile = await client.get_profile(token.access_token)

    assert profile.nombre == "Ana"
    assert call_budget.usage.remaining == 0


async def test_a_zero_limit_disables_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    set_limit(monkeypatch, 0)
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        await client.get_profile(token.access_token)

    usage = call_budget.usage
    assert usage.used == 2
    assert usage.remaining is None
    assert usage.ratio == 0.0


async def test_a_warning_is_logged_near_the_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    set_limit(monkeypatch, 2)
    caplog.set_level(logging.WARNING, logger="ia_trading_broker.iol.budget")
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        await client.get_profile(token.access_token)

    assert "monthly limit" in caplog.text


async def test_the_budget_error_never_contains_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_limit(monkeypatch, 1)
    fake = FakeIOL()

    async with build_client(fake) as client:
        await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLBudgetExceededError) as error:
            await client.get_profile("a-token")

    assert PASSWORD not in str(error.value)
    assert "a-token" not in str(error.value)


async def test_usage_reports_the_period() -> None:
    usage = call_budget.usage

    assert len(usage.period) == 7
    assert usage.period[4] == "-"


async def test_a_non_json_body_is_still_counted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={"access_token": "token", "token_type": "bearer", "expires_in": 900},
            )
        return httpx.Response(200, text="<html>not json</html>")

    async with IOLClient(
        base_url="http://iol.test", transport=httpx.MockTransport(handler)
    ) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLResponseError):
            await client.get_profile(token.access_token)

    assert call_budget.usage.used == 2
