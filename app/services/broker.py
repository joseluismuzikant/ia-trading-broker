"""Orchestration for broker reads.

This module owns the Day 2 rules that involve more than one layer:

* one token per connection, cached in memory;
* exactly one retry after an authentication error;
* a snapshot on every success, and a stale flag on every failure.

No function here logs or returns a password or a bearer token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.iol import (
    IOLAPIError,
    IOLAuthError,
    IOLClient,
    IOLUnavailableError,
    build_client,
    safe_message,
)
from app.infrastructure.iol.tokens import CachedToken, token_store
from app.models import (
    SNAPSHOT_ACCOUNT_STATUS,
    SNAPSHOT_PROFILE,
    BrokerConnection,
    Snapshot,
)
from app.services import connections as connections_service
from app.services import snapshots as snapshots_service

logger = logging.getLogger("ia_trading_broker.broker")

T = TypeVar("T")

SUPPORTED_BROKER = "iol"


@dataclass(frozen=True)
class BrokerReadResult:
    """Outcome of one broker read."""

    kind: str
    ok: bool
    #: The newest snapshot after the call, which may be stale.
    snapshot: Snapshot | None
    #: Safe, short error text when ``ok`` is False.
    error: str | None = None
    #: True when the read failed and older data is still being shown.
    is_stale: bool = False


@dataclass(frozen=True)
class ConnectionTestResult:
    """Outcome of a connection test."""

    ok: bool
    error: str | None = None


async def _token_for(
    client: IOLClient, connection: BrokerConnection, *, refresh_token: str | None = None
) -> CachedToken:
    """Return a usable token, logging in when the cache has nothing."""
    if refresh_token:
        try:
            token = await client.refresh_token(refresh_token)
        except IOLAPIError:
            logger.info(
                "refresh failed for connection id=%s; falling back to a login",
                connection.id,
            )
        else:
            await token_store.set(connection.id, token)
            return token

    password = connections_service.load_password(connection)
    token = await client.fetch_token(
        username=connection.username,
        password=password,
    )
    await token_store.set(connection.id, token)
    return token


async def _call_with_one_retry(
    client: IOLClient,
    connection: BrokerConnection,
    call: Callable[[str], object],
) -> T:
    """Run ``call`` with a valid token, renewing once after a rejection."""
    cached = await token_store.get(connection.id)
    if cached is None:
        token = await _token_for(client, connection)
    else:
        token = cached

    try:
        return await call(token.access_token)  # type: ignore[misc]
    except IOLAuthError:
        # Exactly one retry: refresh the token, or log in again when the
        # refresh token is missing or refused.
        logger.info(
            "token rejected for connection id=%s; renewing once", connection.id
        )
        await token_store.discard(connection.id)
        renewed = await _token_for(
            client, connection, refresh_token=token.refresh_token
        )
        return await call(renewed.access_token)  # type: ignore[misc]


def _ensure_supported(connection: BrokerConnection) -> None:
    if connection.broker != SUPPORTED_BROKER:
        raise IOLUnavailableError(
            f"broker {connection.broker!r} is not supported yet"
        )


async def test_connection(
    db: AsyncSession, *, connection: BrokerConnection
) -> ConnectionTestResult:
    """Authenticate against IOL without reading any account data."""
    try:
        _ensure_supported(connection)
        async with build_client() as client:
            await _call_with_one_retry(
                client, connection, lambda token: _noop(client, token)
            )
    except IOLAPIError as exc:
        safe = safe_message(exc.message)
        await connections_service.record_failure(db, connection, safe)
        logger.warning(
            "connection test failed for connection id=%s: %s", connection.id, safe
        )
        return ConnectionTestResult(ok=False, error=safe)

    await connections_service.record_success(db, connection)
    logger.info("connection test succeeded for connection id=%s", connection.id)
    return ConnectionTestResult(ok=True)


async def _noop(_client: IOLClient, _token: str) -> None:
    """Placeholder call that only proves a token was issued."""
    return None


async def _read(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    kind: str,
    fetch: Callable[[IOLClient, str], object],
) -> BrokerReadResult:
    """Shared read path: fetch, then snapshot or mark stale."""
    try:
        _ensure_supported(connection)
        async with build_client() as client:
            payload = await _call_with_one_retry(
                client, connection, lambda token: fetch(client, token)
            )
    except IOLAPIError as exc:
        safe = safe_message(exc.message)
        await connections_service.record_failure(db, connection, safe)
        snapshot = await snapshots_service.mark_latest_stale(
            db,
            user_id=user_id,
            connection_id=connection.id,
            kind=kind,
            message=safe,
        )
        logger.warning(
            "%s read failed for connection id=%s: %s (stale=%s)",
            kind,
            connection.id,
            safe,
            snapshot is not None,
        )
        return BrokerReadResult(
            kind=kind,
            ok=False,
            snapshot=snapshot,
            error=safe,
            is_stale=snapshot is not None,
        )

    await connections_service.record_success(db, connection)
    snapshot = await snapshots_service.save_snapshot(
        db,
        user_id=user_id,
        connection_id=connection.id,
        kind=kind,
        payload=payload,  # type: ignore[arg-type]
    )
    logger.info(
        "%s snapshot saved for connection id=%s (snapshot id=%s)",
        kind,
        connection.id,
        snapshot.id,
    )
    return BrokerReadResult(kind=kind, ok=True, snapshot=snapshot)


async def read_profile(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection
) -> BrokerReadResult:
    """Refresh and store the IOL profile snapshot."""

    async def fetch(client: IOLClient, token: str) -> dict:
        profile = await client.get_profile(token)
        return profile.model_dump(mode="json", by_alias=True)

    return await _read(
        db, user_id=user_id, connection=connection, kind=SNAPSHOT_PROFILE, fetch=fetch
    )


async def read_account_status(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection
) -> BrokerReadResult:
    """Refresh and store the IOL account-status snapshot."""

    async def fetch(client: IOLClient, token: str) -> dict:
        status = await client.get_account_status(token)
        return status.model_dump(mode="json", by_alias=True)

    return await _read(
        db,
        user_id=user_id,
        connection=connection,
        kind=SNAPSHOT_ACCOUNT_STATUS,
        fetch=fetch,
    )


async def load_snapshot(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection, kind: str
) -> Snapshot | None:
    """Read the newest saved snapshot without contacting the broker."""
    return await snapshots_service.latest_snapshot(
        db, user_id=user_id, connection_id=connection.id, kind=kind
    )
