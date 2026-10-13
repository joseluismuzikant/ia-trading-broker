"""Tests for the IOL HTTP client against a scripted fake transport."""

from __future__ import annotations

import httpx
import pytest

from app.infrastructure.iol.client import IOLClient
from app.infrastructure.iol.errors import (
    IOLAuthError,
    IOLConnectionError,
    IOLResponseError,
)
from tests.fakes import PROFILE_PATH, FakeIOL

USERNAME = "iol-test-user"
PASSWORD = "iol-test-password"


def build_client(fake: FakeIOL) -> IOLClient:
    """An IOL client wired to the fake transport."""
    return IOLClient(base_url="http://iol.test", timeout=5.0, transport=fake.transport)


async def test_fetch_token_returns_a_usable_token() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)

    assert token.access_token
    assert token.refresh_token
    assert not token.is_expired()
    assert fake.token_grants == ["password"]


async def test_wrong_password_raises_auth_error_without_leaking_it() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        with pytest.raises(IOLAuthError) as error:
            await client.fetch_token(username=USERNAME, password="wrong-password")

    message = str(error.value)
    assert "wrong-password" not in message
    assert PASSWORD not in message


async def test_token_response_without_a_token_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "bearer", "expires_in": 60})

    async with IOLClient(
        base_url="http://iol.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(IOLResponseError):
            await client.fetch_token(username=USERNAME, password=PASSWORD)


async def test_get_profile_parses_the_documented_fields() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        profile = await client.get_profile(token.access_token)

    assert profile.nombre == "Ana"
    assert profile.numero_cuenta == "123456"
    assert profile.perfil_inversor == "Moderado"
    assert profile.full_name == "Ana Perez"
    assert fake.authenticated_paths == [PROFILE_PATH]


async def test_get_account_status_parses_accounts_and_total() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        status = await client.get_account_status(token.access_token)

    assert status.total_en_pesos == 200.0
    assert len(status.cuentas) == 1
    assert status.cuentas[0].moneda == "Peso_Argentino"
    assert status.cuentas[0].saldos[0].liquidacion == "Inmediato"


async def test_a_rejected_token_raises_auth_error() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        with pytest.raises(IOLAuthError):
            await client.get_profile("a-token-the-broker-never-issued")


async def test_server_error_raises_response_error() -> None:
    fake = FakeIOL(profile_error_status=500)

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLResponseError) as error:
            await client.get_profile(token.access_token)

    assert "500" in str(error.value)


async def test_unexpected_shape_names_the_field_and_not_the_value() -> None:
    # A nested object where a scalar belongs: a structural mismatch, since
    # plain numbers are accepted and normalised for display fields.
    fake = FakeIOL(profile={"nombre": {"leaked-value-xyz": True}})

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLResponseError) as error:
            await client.get_profile(token.access_token)

    message = str(error.value)
    assert "nombre" in message
    assert "leaked-value-xyz" not in message


async def test_numeric_text_fields_are_normalised_to_strings() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        status = await client.get_account_status(token.access_token)

    # The fixture sends ``"numero": 2`` as a number.
    assert status.cuentas[0].numero == "2"


async def test_money_fields_accept_numbers_or_numeric_strings() -> None:
    fake = FakeIOL(
        account_status={
            "cuentas": [{"numero": "7", "disponible": "150.25", "total": 200}],
            "estadisticas": [],
            "totalEnPesos": "350.75",
        }
    )

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        status = await client.get_account_status(token.access_token)

    assert status.cuentas[0].numero == "7"
    assert status.cuentas[0].disponible == 150.25
    assert status.cuentas[0].total == 200.0
    assert status.total_en_pesos == 350.75


async def test_non_json_body_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "token",
                    "token_type": "bearer",
                    "expires_in": 900,
                },
            )
        return httpx.Response(200, text="<html>not json</html>")

    async with IOLClient(
        base_url="http://iol.test", transport=httpx.MockTransport(handler)
    ) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLResponseError):
            await client.get_profile(token.access_token)


async def test_network_failure_raises_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with IOLClient(
        base_url="http://iol.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(IOLConnectionError):
            await client.fetch_token(username=USERNAME, password=PASSWORD)


async def test_timeout_raises_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    async with IOLClient(
        base_url="http://iol.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(IOLConnectionError):
            await client.fetch_token(username=USERNAME, password=PASSWORD)


async def test_error_messages_never_contain_the_token() -> None:
    fake = FakeIOL(account_status_error_status=503)

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLResponseError) as error:
            await client.get_account_status(token.access_token)

    assert token.access_token not in str(error.value)
