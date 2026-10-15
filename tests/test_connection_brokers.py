"""Tests for per-broker connections: what is asked, how it is stored, who can read it."""

from __future__ import annotations

import json

import pytest

from app.crypto import decrypt_secret
from app.services import connections as connections_service
from app.services.connections import (
    ConnectionAlreadyExistsError,
    ConnectionNotFoundError,
    ConnectionValidationError,
)
from tests.helpers import extract_csrf, login

PPI_FIELDS = {
    "account_number": "ACC-0001",
    "api_key": "ppi-public-key-value",
    "api_secret": "ppi-private-secret-value",
}


async def make_ppi(db_session, user, **overrides):
    data = {
        "user_id": user.id,
        "broker": "ppi",
        "label": "My PPI account",
        "fields": dict(PPI_FIELDS),
    }
    data.update(overrides)
    return await connections_service.create_broker_connection(db_session, **data)


async def test_a_ppi_connection_stores_every_field_encrypted(db_session, make_user) -> None:
    user = await make_user("alice")

    connection = await make_ppi(db_session, user)

    assert connection.broker == "ppi"
    assert connection.password_encrypted == ""
    assert connection.username == "ACC-0001"  # the non-secret field shown as the login
    sealed = connection.credentials_encrypted
    assert sealed and "ppi-private-secret-value" not in sealed and "ppi-public-key" not in sealed
    assert json.loads(decrypt_secret(sealed))["fields"] == PPI_FIELDS


async def test_the_credentials_of_a_connection_are_read_back_by_the_service_only(
    db_session, make_user
) -> None:
    user = await make_user("alice")
    ppi = await make_ppi(db_session, user)
    iol = await connections_service.create_connection(
        db_session,
        user_id=user.id,
        label="My IOL account",
        username="iol-test-user",
        password="iol-secret-password",
    )

    assert connections_service.load_credentials(ppi) == PPI_FIELDS
    assert connections_service.load_credentials(iol) == {
        "username": "iol-test-user",
        "password": "iol-secret-password",
    }


@pytest.mark.parametrize("missing", ["account_number", "api_key", "api_secret"])
async def test_every_broker_field_is_required(db_session, make_user, missing) -> None:
    user = await make_user("alice")
    fields = {k: v for k, v in PPI_FIELDS.items() if k != missing}

    with pytest.raises(ConnectionValidationError, match="is required"):
        await make_ppi(db_session, user, fields=fields)


async def test_an_unknown_broker_is_refused(db_session, make_user) -> None:
    user = await make_user("alice")

    with pytest.raises(ConnectionValidationError, match="unknown broker"):
        await make_ppi(db_session, user, broker="nope")


async def test_iol_has_its_own_form_and_is_refused_by_the_generic_one(
    db_session, make_user
) -> None:
    user = await make_user("alice")

    with pytest.raises(ConnectionValidationError, match="IOL connection form"):
        await make_ppi(db_session, user, broker="iol", fields={})


async def test_a_label_can_only_be_used_once_per_user(db_session, make_user) -> None:
    user = await make_user("alice")
    await make_ppi(db_session, user)

    with pytest.raises(ConnectionAlreadyExistsError):
        await make_ppi(db_session, user)


async def test_a_real_broker_cannot_take_prices_from_another_connection(
    db_session, make_user
) -> None:
    user = await make_user("alice")
    other = await make_ppi(db_session, user, label="Other")

    with pytest.raises(ConnectionValidationError, match="only a paper account"):
        await make_ppi(db_session, user, price_source_connection_id=other.id)


async def test_another_users_connection_is_not_found(db_session, make_user) -> None:
    alice = await make_user("alice")
    mallory = await make_user("mallory")
    ppi = await make_ppi(db_session, alice)

    with pytest.raises(ConnectionNotFoundError):
        await connections_service.get_connection(
            db_session, user_id=mallory.id, connection_id=ppi.id
        )


# --- The form -----------------------------------------------------------------


async def test_the_form_asks_for_the_fields_of_the_chosen_broker(
    client, make_user, password
) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)

    iol = await client.get("/connections/new")
    ppi = await client.get("/connections/new?broker=ppi")

    assert "IOL username" in iol.text and "Public API key" not in iol.text
    assert "Public API key" in ppi.text and "Private API key" in ppi.text
    assert "IOL password" not in ppi.text


async def test_a_ppi_connection_is_created_through_the_form_and_never_shows_a_secret(
    client, db_session, make_user, password
) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)
    page = await client.get("/connections/new?broker=ppi")

    response = await client.post(
        "/connections",
        data={
            "broker": "ppi",
            "label": "My PPI account",
            "country": "argentina",
            "csrf_token": extract_csrf(page.text),
            **PPI_FIELDS,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    detail = await client.get(response.headers["location"])
    assert detail.status_code == 200
    for secret in ("ppi-public-key-value", "ppi-private-secret-value"):
        assert secret not in detail.text


async def test_a_refused_form_is_shown_again_without_the_secrets(
    client, make_user, password
) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)
    page = await client.get("/connections/new?broker=ppi")

    response = await client.post(
        "/connections",
        data={
            "broker": "ppi",
            "label": "My PPI account",
            "country": "argentina",
            "csrf_token": extract_csrf(page.text),
            "account_number": "ACC-0001",
            "api_key": "ppi-public-key-value",
            "api_secret": "",  # missing, so the form is refused
        },
    )

    assert response.status_code == 400
    assert "is required" in response.text
    assert "ACC-0001" in response.text  # the non-secret field is kept
    assert "ppi-public-key-value" not in response.text  # a secret never comes back


async def test_an_unknown_broker_in_the_form_is_a_clear_error(client, make_user, password) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)
    page = await client.get("/connections/new")

    response = await client.post(
        "/connections",
        data={"broker": "nope", "label": "x", "csrf_token": extract_csrf(page.text)},
    )

    assert response.status_code == 400
    assert "unknown broker" in response.text


async def test_the_form_needs_a_csrf_token(client, make_user, password) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)

    response = await client.post("/connections", data={"broker": "ppi", "label": "x", **PPI_FIELDS})

    assert response.status_code in (400, 403)
