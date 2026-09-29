"""Service-level tests for saved broker connections."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.crypto import decrypt_secret
from app.models import BrokerConnection, Snapshot
from app.services import connections as connections_service
from app.services import snapshots as snapshots_service
from app.services.connections import (
    ConnectionAlreadyExistsError,
    ConnectionNotFoundError,
    ConnectionValidationError,
)

IOL_PASSWORD = "iol-super-secret-password"


async def make_connection(db_session, user, **overrides):
    """Create a connection with sensible defaults."""
    data = {
        "user_id": user.id,
        "label": "My IOL account",
        "username": "iol-test-user",
        "password": IOL_PASSWORD,
        "country": "argentina",
    }
    data.update(overrides)
    return await connections_service.create_connection(db_session, **data)


# --- Storage --------------------------------------------------------------


async def test_password_is_stored_encrypted_and_recoverable(db_session, make_user) -> None:
    user = await make_user("alice")

    connection = await make_connection(db_session, user)

    assert IOL_PASSWORD not in connection.password_encrypted
    assert connection.password_encrypted != IOL_PASSWORD
    assert connections_service.load_password(connection) == IOL_PASSWORD


async def test_username_is_trimmed_and_label_is_collapsed(db_session, make_user) -> None:
    user = await make_user("alice")

    connection = await make_connection(
        db_session,
        user,
        label="  My   IOL   account  ",
        username="  iol-test-user  ",
    )

    assert connection.label == "My IOL account"
    assert connection.username == "iol-test-user"


async def test_default_broker_and_active_flag(db_session, make_user) -> None:
    user = await make_user("alice")

    connection = await make_connection(db_session, user)

    assert connection.broker == "iol"
    assert connection.is_active is True
    assert connection.last_error is None
    assert connection.last_used_at is None


async def test_country_is_normalised(db_session, make_user) -> None:
    user = await make_user("alice")

    connection = await make_connection(db_session, user, country="  Estados Unidos ")

    assert connection.country == "estados_unidos"


# --- Validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"label": "   "},
        {"username": "   "},
        {"password": ""},
        {"country": "atlantis"},
        {"label": "x" * 81},
        {"username": "x" * 121},
    ],
)
async def test_invalid_input_is_rejected(db_session, make_user, overrides) -> None:
    user = await make_user("alice")

    with pytest.raises(ConnectionValidationError):
        await make_connection(db_session, user, **overrides)


async def test_duplicate_label_for_the_same_user_is_rejected(db_session, make_user) -> None:
    user = await make_user("alice")
    await make_connection(db_session, user, label="Duplicated")

    with pytest.raises(ConnectionAlreadyExistsError):
        await make_connection(db_session, user, label="Duplicated")


async def test_two_users_may_reuse_the_same_label(db_session, make_user) -> None:
    alice = await make_user("alice")
    bob = await make_user("bob")

    await make_connection(db_session, alice, label="Shared label")
    second = await make_connection(db_session, bob, label="Shared label")

    assert second.id is not None


# --- Tenant isolation -----------------------------------------------------


async def test_a_user_cannot_load_another_users_connection(db_session, make_user) -> None:
    alice = await make_user("alice")
    bob = await make_user("bob")
    connection = await make_connection(db_session, alice)

    with pytest.raises(ConnectionNotFoundError):
        await connections_service.get_connection(
            db_session, user_id=bob.id, connection_id=connection.id
        )


async def test_listing_only_returns_own_connections(db_session, make_user) -> None:
    alice = await make_user("alice")
    bob = await make_user("bob")
    await make_connection(db_session, alice, label="Alice account")
    await make_connection(db_session, bob, label="Bob account")

    alice_connections = await connections_service.list_connections(
        db_session, user_id=alice.id
    )

    assert [item.label for item in alice_connections] == ["Alice account"]


async def test_deleting_another_users_connection_is_refused(db_session, make_user) -> None:
    alice = await make_user("alice")
    bob = await make_user("bob")
    connection = await make_connection(db_session, alice)

    with pytest.raises(ConnectionNotFoundError):
        await connections_service.delete_connection(
            db_session, user_id=bob.id, connection_id=connection.id
        )

    still_there = await connections_service.get_connection(
        db_session, user_id=alice.id, connection_id=connection.id
    )
    assert still_there.id == connection.id


# --- Failure bookkeeping --------------------------------------------------


async def test_record_failure_stores_a_short_safe_message(db_session, make_user) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user)

    await connections_service.record_failure(db_session, connection, "long" * 100)

    assert connection.last_error is not None
    assert len(connection.last_error) <= 200
    assert connection.last_error_at is not None


async def test_record_success_clears_the_previous_error(db_session, make_user) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user)
    await connections_service.record_failure(db_session, connection, "broken")

    await connections_service.record_success(db_session, connection)

    assert connection.last_error is None
    assert connection.last_error_at is None
    assert connection.last_used_at is not None


# --- Deletion -------------------------------------------------------------


async def test_delete_removes_the_connection_and_its_snapshots(
    db_session, make_user
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user)
    await snapshots_service.save_snapshot(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        kind="profile",
        payload={"nombre": "Ana"},
    )

    await connections_service.delete_connection(
        db_session, user_id=user.id, connection_id=connection.id
    )

    remaining = await db_session.execute(select(BrokerConnection))
    assert list(remaining.scalars()) == []
    orphaned = await db_session.execute(select(Snapshot))
    assert list(orphaned.scalars()) == []
