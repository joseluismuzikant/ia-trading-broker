"""Tests for the PPI client: headers, session handling and error safety.

Every test runs against an ``httpx.MockTransport``; no PPI endpoint is reachable.
The response bodies follow ``ppi_rest_openapi_with_examples.json`` (kept in the
git-ignored ``postman/`` folder), whose
examples are illustrative, so these tests pin the adapter's handling of those
shapes and not PPI's real behaviour.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.infrastructure.ppi.client import PPIClient
from app.infrastructure.ppi.errors import (
    PPIAuthError,
    PPIResponseError,
    PPIUnavailableError,
)

TOKEN = {
    "accessToken": "access-123",
    "refreshToken": "refresh-456",
    "tokenType": "bearer",
    "expirationDate": "2026-10-13T20:00:00Z",
}


class Recorder:
    """A transport that records requests and answers from a function."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)


def client_for(recorder: Recorder) -> PPIClient:
    return PPIClient(
        base_url="https://ppi.test",
        authorized_client="auth-client",
        client_key="client-key",
        transport=httpx.MockTransport(recorder),
    )


def test_the_client_refuses_to_build_without_integration_headers() -> None:
    with pytest.raises(PPIUnavailableError):
        PPIClient(base_url="https://ppi.test", authorized_client="", client_key="k")


async def test_login_sends_the_keys_only_on_the_login_call() -> None:
    recorder = Recorder(lambda request: httpx.Response(200, json=TOKEN))
    async with client_for(recorder) as client:
        token = await client.login(api_key="public", api_secret="private")

    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/1.0/Account/LoginApi"
    assert request.headers["ApiKey"] == "public"
    assert request.headers["ApiSecret"] == "private"
    assert request.headers["AuthorizedClient"] == "auth-client"
    assert request.headers["ClientKey"] == "client-key"
    assert "Authorization" not in request.headers
    assert token.access_token == "access-123"
    assert token.refresh_token == "refresh-456"


async def test_login_accepts_the_list_shape_of_the_openapi_file() -> None:
    recorder = Recorder(lambda request: httpx.Response(200, json=[TOKEN]))
    async with client_for(recorder) as client:
        token = await client.login(api_key="a", api_secret="b")
    assert token.access_token == "access-123"


async def test_later_calls_carry_the_bearer_token_and_never_the_keys() -> None:
    recorder = Recorder(
        lambda request: httpx.Response(200, json=[{"accountNumber": "123", "name": "Ana"}])
    )
    async with client_for(recorder) as client:
        accounts = await client.accounts("access-123")

    request = recorder.requests[0]
    assert request.headers["Authorization"] == "Bearer access-123"
    assert "ApiSecret" not in request.headers
    assert accounts[0].account_number == "123"


async def test_refresh_posts_the_refresh_token() -> None:
    recorder = Recorder(lambda request: httpx.Response(200, json=TOKEN))
    async with client_for(recorder) as client:
        await client.refresh(access_token="old", refresh_token="refresh-456")

    request = recorder.requests[0]
    assert request.url.path == "/api/1.0/Account/RefreshToken"
    assert json.loads(request.content) == {"refreshToken": "refresh-456"}
    assert request.headers["Authorization"] == "Bearer old"


async def test_a_wrong_token_type_is_refused() -> None:
    body = {**TOKEN, "tokenType": "mac"}
    recorder = Recorder(lambda request: httpx.Response(200, json=body))
    async with client_for(recorder) as client:
        with pytest.raises(PPIResponseError):
            await client.login(api_key="a", api_secret="b")


async def test_a_login_refusal_is_an_auth_error_without_secrets() -> None:
    recorder = Recorder(lambda request: httpx.Response(401, json={"message": "bad"}))
    async with client_for(recorder) as client:
        with pytest.raises(PPIAuthError) as caught:
            await client.login(api_key="public-key", api_secret="private-secret")
    assert "private-secret" not in str(caught.value)
    assert "public-key" not in str(caught.value)


async def test_a_malformed_body_reports_field_names_not_values() -> None:
    recorder = Recorder(
        lambda request: httpx.Response(200, json=[{"accountNumber": "", "name": "secret-name"}])
    )
    async with client_for(recorder) as client:
        with pytest.raises(PPIResponseError) as caught:
            await client.accounts("t")
    assert "secret-name" not in str(caught.value)
    assert "accountNumber" in str(caught.value)


async def test_available_balance_uses_decimals() -> None:
    body = [{"name": "Pesos", "simbol": "ARS", "amount": 1234.5, "settlement": "INMEDIATA"}]
    recorder = Recorder(lambda request: httpx.Response(200, json=body))
    async with client_for(recorder) as client:
        balances = await client.available_balance("t", "123")

    assert recorder.requests[0].url.params["accountNumber"] == "123"
    assert balances[0].amount == Decimal("1234.5")
    assert balances[0].simbol == "ARS"


async def test_quotes_and_history_use_the_documented_parameters() -> None:
    point = {"date": "2026-10-13T00:00:00Z", "price": 240.5, "volume": 10, "max": 245, "min": 238}
    recorder = Recorder(
        lambda request: httpx.Response(
            200, json=point if request.url.path.endswith("Current") else [point]
        )
    )
    async with client_for(recorder) as client:
        quote = await client.current_quote(
            "t", ticker="GGAL", instrument_type="ACCIONES", settlement="INMEDIATA"
        )
        history = await client.price_history(
            "t",
            ticker="GGAL",
            instrument_type="ACCIONES",
            settlement="INMEDIATA",
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 13),
        )

    current, search = recorder.requests
    assert dict(current.url.params) == {
        "Ticker": "GGAL",
        "Type": "ACCIONES",
        "Settlement": "INMEDIATA",
    }
    assert search.url.params["DateFrom"] == "2026-10-01T00:00:00Z"
    assert quote.price == Decimal("240.5")
    assert history[0].max == Decimal("245")


async def test_a_transport_failure_on_a_read_is_a_connection_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    async with client_for(Recorder(boom)) as client:
        with pytest.raises(Exception) as caught:
            await client.accounts("t")
    assert "did not answer in time" in str(caught.value)
