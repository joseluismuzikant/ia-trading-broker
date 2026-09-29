"""Broker connection rules: saving, testing, and deleting IOL logins."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import CredentialEncryptionError, decrypt_secret, encrypt_secret
from app.models import BrokerConnection

logger = logging.getLogger("ia_trading_broker.connections")

#: Countries IOL exposes a portfolio for. Day 2 only needs the value to be
#: validated; the country portfolio page arrives on Day 3.
SUPPORTED_COUNTRIES = ("argentina", "estados_unidos")

MAX_LABEL_LENGTH = 80
MAX_USERNAME_LENGTH = 120


class ConnectionError_(Exception):
    """Base class for connection service failures."""


class ConnectionValidationError(ConnectionError_):
    """The submitted connection data is not usable."""


class ConnectionAlreadyExistsError(ConnectionError_):
    """The user already saved a connection with that label."""


class ConnectionNotFoundError(ConnectionError_):
    """No connection matched the requested id for this user."""


def normalize_label(label: str) -> str:
    """Trim and collapse a connection label."""
    return " ".join(label.split())


def normalize_username(username: str) -> str:
    """Trim the IOL username. It is stored as typed, only trimmed."""
    return username.strip()


def validate_country(country: str) -> str:
    """Return a lowercase country value or raise."""
    value = country.strip().lower().replace(" ", "_")
    if value not in SUPPORTED_COUNTRIES:
        raise ConnectionValidationError(
            f"country must be one of: {', '.join(SUPPORTED_COUNTRIES)}"
        )
    return value


async def list_connections(db: AsyncSession, *, user_id: int) -> list[BrokerConnection]:
    """Return every connection owned by the user, oldest first."""
    result = await db.execute(
        select(BrokerConnection)
        .where(BrokerConnection.user_id == user_id)
        .order_by(BrokerConnection.created_at, BrokerConnection.id)
    )
    return list(result.scalars())


async def get_connection(
    db: AsyncSession, *, user_id: int, connection_id: int
) -> BrokerConnection:
    """Return one connection owned by the user.

    The ``user_id`` filter is what keeps one user away from another user's
    broker login; a mismatch behaves exactly like a missing row.
    """
    result = await db.execute(
        select(BrokerConnection).where(
            BrokerConnection.id == connection_id,
            BrokerConnection.user_id == user_id,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise ConnectionNotFoundError("Connection not found.")
    return connection


async def label_is_taken(db: AsyncSession, *, user_id: int, label: str) -> bool:
    """Return True when this user already saved a connection with that label."""
    result = await db.execute(
        select(BrokerConnection.id).where(
            BrokerConnection.user_id == user_id,
            BrokerConnection.label == normalize_label(label),
        )
    )
    return result.scalar_one_or_none() is not None


async def create_connection(
    db: AsyncSession,
    *,
    user_id: int,
    label: str,
    username: str,
    password: str,
    country: str = "argentina",
) -> BrokerConnection:
    """Encrypt the password and save a new connection."""
    clean_label = normalize_label(label)
    clean_username = normalize_username(username)
    clean_country = validate_country(country)

    if not clean_label:
        raise ConnectionValidationError("label is required")
    if len(clean_label) > MAX_LABEL_LENGTH:
        raise ConnectionValidationError("label is too long")
    if not clean_username:
        raise ConnectionValidationError("username is required")
    if len(clean_username) > MAX_USERNAME_LENGTH:
        raise ConnectionValidationError("username is too long")
    if not password:
        raise ConnectionValidationError("password is required")

    try:
        encrypted = encrypt_secret(password)
    except CredentialEncryptionError as exc:
        raise ConnectionValidationError(str(exc)) from exc

    # Checked up front so a repeated submit is refused without touching the
    # database. The IntegrityError handler below still covers a true race.
    if await label_is_taken(db, user_id=user_id, label=clean_label):
        raise ConnectionAlreadyExistsError(
            "A connection with that label already exists."
        )

    connection = BrokerConnection(
        user_id=user_id,
        broker="iol",
        label=clean_label,
        country=clean_country,
        username=clean_username,
        password_encrypted=encrypted,
    )
    db.add(connection)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConnectionAlreadyExistsError(
            "A connection with that label already exists."
        ) from exc

    await db.refresh(connection)
    logger.info(
        "saved broker connection id=%s user_id=%s broker=%s",
        connection.id,
        user_id,
        connection.broker,
    )
    return connection


def load_password(connection: BrokerConnection) -> str:
    """Decrypt the stored password for one broker call.

    The return value must never be logged, stored, or rendered.
    """
    return decrypt_secret(connection.password_encrypted)


async def record_success(db: AsyncSession, connection: BrokerConnection) -> None:
    """Clear the previous error and mark the connection as just used."""
    connection.last_used_at = datetime.now(UTC)
    connection.last_error = None
    connection.last_error_at = None
    await db.commit()


async def record_failure(
    db: AsyncSession, connection: BrokerConnection, message: str
) -> None:
    """Store a short, safe description of a failed broker call."""
    connection.last_used_at = datetime.now(UTC)
    connection.last_error = message[:200]
    connection.last_error_at = datetime.now(UTC)
    await db.commit()


async def delete_connection(
    db: AsyncSession, *, user_id: int, connection_id: int
) -> None:
    """Delete a connection and its snapshots."""
    connection = await get_connection(db, user_id=user_id, connection_id=connection_id)
    await db.delete(connection)
    await db.commit()
