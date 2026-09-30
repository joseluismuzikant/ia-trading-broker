"""Orchestration for broker reads.

This module owns the rules that involve more than one layer:

* one token per connection, cached in memory;
* exactly one retry after an authentication error;
* a snapshot on every success, and a stale flag on every failure;
* a country portfolio stored in the shared, broker-agnostic format.

No function here logs or returns a password or a bearer token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, TypeVar

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.iol import (
    AccountStatus,
    IOLAPIError,
    IOLAuthError,
    IOLClient,
    IOLUnavailableError,
    Quote,
    build_client,
    portfolio_currency,
    safe_message,
    to_portfolio,
)
from app.infrastructure.iol.tokens import CachedToken, token_store
from app.models import (
    SNAPSHOT_ACCOUNT_STATUS,
    SNAPSHOT_PROFILE,
    BrokerConnection,
    Snapshot,
    portfolio_snapshot_kind,
    price_history_kind,
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


async def _cash_from_account_status(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    currency: str | None,
) -> float | None:
    """Free cash for one currency, taken from the saved account status.

    The country-portfolio endpoint does not report cash, so it is read from the
    newest account-status snapshot (Day 2 data) instead of spending a second
    broker call. Returns None when nothing is saved, the saved payload cannot
    be read, or no account matches the currency.
    """
    snapshot = await snapshots_service.latest_snapshot(
        db,
        user_id=user_id,
        connection_id=connection.id,
        kind=SNAPSHOT_ACCOUNT_STATUS,
    )
    if snapshot is None:
        return None

    try:
        status = AccountStatus.model_validate(snapshot.payload)
    except ValidationError:
        return None

    total = 0.0
    matched = False
    for account in status.cuentas:
        if currency and account.moneda and account.moneda.lower() != currency.lower():
            continue
        if account.disponible is None:
            continue
        total += account.disponible
        matched = True

    return total if matched else None


async def read_portfolio(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
) -> BrokerReadResult:
    """Refresh one country portfolio and store it in the shared format."""
    clean_country = connections_service.validate_country(country)

    async def fetch(client: IOLClient, token: str) -> dict:
        raw = await client.get_portfolio(token, clean_country)
        # A cheap indexed read. It may repeat if the token is renewed mid-call,
        # which is harmless because it only reads.
        cash = await _cash_from_account_status(
            db,
            user_id=user_id,
            connection=connection,
            currency=portfolio_currency(raw),
        )
        portfolio = to_portfolio(clean_country, raw, cash=cash)
        return portfolio.model_dump(mode="json")

    return await _read(
        db,
        user_id=user_id,
        connection=connection,
        kind=portfolio_snapshot_kind(clean_country),
        fetch=fetch,
    )


#: Days of price history an analysis reads. Long enough for the slow EMA (26)
#: plus the MACD signal line (9), with room to spare.
PRICE_HISTORY_DAYS = 180


async def read_price_history(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    market: str,
    symbol: str,
    days: int = PRICE_HISTORY_DAYS,
) -> BrokerReadResult:
    """Refresh one instrument's price history and store it for analysis.

    The history is saved per symbol, so refreshing one instrument never
    overwrites another's. The payload holds the list of quotes and nothing else.
    """
    clean_symbol = symbol.strip().upper()
    clean_market = market.strip()
    if not clean_symbol or not clean_market:
        raise IOLUnavailableError("a market and a symbol are required")

    date_to = datetime.now(UTC).date()
    date_from = date_to - timedelta(days=days)

    async def fetch(client: IOLClient, token: str) -> dict:
        quotes = await client.get_price_history(
            token,
            market=clean_market,
            symbol=clean_symbol,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )
        return {
            "symbol": clean_symbol,
            "market": clean_market,
            "quotes": [quote.model_dump(mode="json", by_alias=True) for quote in quotes],
        }

    return await _read(
        db,
        user_id=user_id,
        connection=connection,
        kind=price_history_kind(clean_symbol),
        fetch=fetch,
    )


def quotes_from_snapshot(snapshot: Snapshot) -> list[Quote]:
    """Read the quotes out of a saved price-history snapshot."""
    payload = snapshot.payload.get("quotes", [])
    return [Quote.model_validate(item) for item in payload]


async def load_snapshot(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection, kind: str
) -> Snapshot | None:
    """Read the newest saved snapshot without contacting the broker."""
    return await snapshots_service.latest_snapshot(
        db, user_id=user_id, connection_id=connection.id, kind=kind
    )
