"""Orchestration for broker reads.

This module owns the rules that involve more than one layer:

* which adapter reads a given connection (``open_reader``);
* a snapshot on every success, and a stale flag on every failure;
* a country portfolio stored in the shared, broker-agnostic format.

Logging in, the token cache and the one renewal after a rejected token belong
to the broker's own adapter (for IOL, :class:`~app.infrastructure.iol.IOLBroker`).
No function here logs or returns a password or a bearer token.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain import brokers as broker_kinds
from app.domain.money import money
from app.domain.universe import get_universe
from app.infrastructure.iol import (
    AccountStatus,
    IOLBroker,
    Quote,
    build_client,
    price_bar_from_quote,
    safe_message,
)
from app.infrastructure.ppi.broker import PPIBroker
from app.infrastructure.ppi.client import build_client as build_ppi_client
from app.infrastructure.simulation import PaperBroker
from app.models import (
    SNAPSHOT_ACCOUNT_STATUS,
    SNAPSHOT_PROFILE,
    BrokerConnection,
    Snapshot,
    portfolio_snapshot_kind,
    price_history_kind,
)
from app.ports.broker_reader import (
    BrokerError,
    BrokerReader,
    BrokerUnavailableError,
    PriceBar,
)
from app.services import connections as connections_service
from app.services import ledger as ledger_service
from app.services import snapshots as snapshots_service

logger = logging.getLogger("ia_trading_broker.broker")

SUPPORTED_BROKER = "iol"


async def _open_iol(connection: BrokerConnection, db: AsyncSession | None) -> IOLBroker:
    """An IOL reader. The password is decrypted only when a login is needed."""
    return IOLBroker(
        connection_id=connection.id,
        username=connection.username,
        password_provider=lambda: connections_service.load_password(connection),
        client=build_client(),
    )


async def _open_paper(connection: BrokerConnection, db: AsyncSession | None) -> PaperBroker:
    """A paper account, priced through one of the owner's real connections."""
    if db is None:
        raise BrokerUnavailableError("a paper account can only be read with a database session")

    price_source: BrokerReader | None = None
    if connection.price_source_connection_id is not None:
        try:
            source = await connections_service.get_connection(
                db,
                user_id=connection.user_id,
                connection_id=connection.price_source_connection_id,
            )
        except connections_service.ConnectionNotFoundError as exc:
            raise BrokerUnavailableError(
                "The connection this paper account takes prices from no longer exists."
            ) from exc
        if broker_kinds.is_simulated(source.broker):
            raise BrokerUnavailableError("A paper account cannot take prices from another one.")
        price_source = await open_reader(source, db=db)

    async def load_account(country: str):
        return await ledger_service.load_paper_account(
            db, user_id=connection.user_id, connection=connection, country=country
        )

    return PaperBroker(
        country=connection.country, load_account=load_account, price_source=price_source
    )


#: PPI instrument types known to be accepted, by the universe category they cover.
#: Only shares are confirmed; everything else is refused rather than guessed.
_PPI_INSTRUMENT_TYPES = {"argentina_stocks": "ACCIONES"}


def _ppi_instrument_type(symbol: str) -> str | None:
    """PPI's instrument type for a symbol, from its universe category, if confirmed."""
    universe = get_universe(get_settings().universe_config_path or None)
    instrument = universe.find(symbol)
    return _PPI_INSTRUMENT_TYPES.get(instrument.category) if instrument else None


async def _open_ppi(connection: BrokerConnection, db: AsyncSession | None) -> PPIBroker:
    """A PPI reader. The keys are decrypted only when a login is needed."""

    def keys() -> tuple[str, str]:
        fields = connections_service.load_credentials(connection)
        return fields["api_key"], fields["api_secret"]

    return PPIBroker(
        connection_id=connection.id,
        account_number=connections_service.load_credentials(connection)["account_number"],
        credentials_provider=keys,
        client=build_ppi_client(),
        instrument_type_for=_ppi_instrument_type,
    )


#: One entry per broker a connection can name. Adding a broker is adding an
#: adapter and one line here; nothing else in the services changes.
_READER_FACTORIES: dict[
    str, Callable[[BrokerConnection, AsyncSession | None], Awaitable[BrokerReader]]
] = {
    broker_kinds.IOL: _open_iol,
    broker_kinds.PPI: _open_ppi,
    broker_kinds.PAPER: _open_paper,
}


async def open_reader(
    connection: BrokerConnection, *, db: AsyncSession | None = None
) -> BrokerReader:
    """The adapter that reads this connection. The caller closes it."""
    factory = _READER_FACTORIES.get(connection.broker)
    if factory is None:
        raise BrokerUnavailableError(f"broker {connection.broker!r} is not supported yet")
    return await factory(connection, db)


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


async def trading_session(
    connection: BrokerConnection,
) -> tuple[object, str, Callable[[], Awaitable[str]]]:
    """An authenticated client and a token renewer for the trading writes.

    Reads are retried carefully; writes are not. The renewer exists only so the
    live order executor may retry exactly once after a *provable* token refusal
    (401/403, where the broker placed nothing). No caller may use it to resend
    an order whose outcome is unknown. The caller owns the client and must
    close it when the session is done.

    Live orders are only wired for IOL; any other broker is refused here.
    """
    kind = broker_kinds.kind_of(connection.broker)
    if kind is None or not kind.can_trade:
        raise BrokerUnavailableError(
            f"live execution is not available for {connection.broker!r} connections"
        )
    reader = await open_reader(connection)
    if not isinstance(reader, IOLBroker):  # pragma: no cover - can_trade implies IOL today
        await reader.aclose()
        raise BrokerUnavailableError("live execution is only wired for IOL")
    return reader.client, await reader.access_token(), reader.renew_access_token


async def test_connection(
    db: AsyncSession, *, connection: BrokerConnection
) -> ConnectionTestResult:
    """Authenticate against the broker without reading any account data."""
    try:
        reader = await open_reader(connection, db=db)
        try:
            await reader.authenticate()
        finally:
            await reader.aclose()
    except BrokerError as exc:
        safe = safe_message(exc.message)
        await connections_service.record_failure(db, connection, safe)
        logger.warning("connection test failed for connection id=%s: %s", connection.id, safe)
        return ConnectionTestResult(ok=False, error=safe)

    await connections_service.record_success(db, connection)
    logger.info("connection test succeeded for connection id=%s", connection.id)
    return ConnectionTestResult(ok=True)


async def _read(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    kind: str,
    fetch: Callable[[BrokerReader], Awaitable[dict]],
) -> BrokerReadResult:
    """Shared read path: fetch, then snapshot or mark stale."""
    try:
        reader = await open_reader(connection, db=db)
        try:
            payload = await fetch(reader)
        finally:
            await reader.aclose()
    except BrokerError as exc:
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
        payload=payload,
    )
    logger.info(
        "%s snapshot saved for connection id=%s (snapshot id=%s)",
        kind,
        connection.id,
        snapshot.id,
    )
    return BrokerReadResult(kind=kind, ok=True, snapshot=snapshot)


def _iol_only(reader: BrokerReader, method: str):
    """An IOL-specific read, or an error when this broker has no such page."""
    call = getattr(reader, method, None)
    if call is None:
        raise BrokerUnavailableError("this broker does not provide that information")
    return call


async def read_profile(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection
) -> BrokerReadResult:
    """Refresh and store the broker's profile snapshot (IOL only)."""

    async def fetch(reader: BrokerReader) -> dict:
        return await _iol_only(reader, "get_profile_payload")()

    return await _read(
        db, user_id=user_id, connection=connection, kind=SNAPSHOT_PROFILE, fetch=fetch
    )


async def read_account_status(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection
) -> BrokerReadResult:
    """Refresh and store the broker's account-status snapshot (IOL only)."""

    async def fetch(reader: BrokerReader) -> dict:
        return await _iol_only(reader, "get_account_status_payload")()

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


async def available_cash(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    currency: str | None = None,
):
    """The free cash the broker reports, from the newest account-status read.

    This is the money a purchase may actually spend: it never counts proceeds
    from a sale the broker has not settled. Returns None when no account
    status is saved yet.
    """
    value = await _cash_from_account_status(
        db, user_id=user_id, connection=connection, currency=currency
    )
    return money(value) if value is not None else None


async def read_portfolio(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
) -> BrokerReadResult:
    """Refresh one country portfolio and store it in the shared format."""
    clean_country = connections_service.validate_country(country)

    async def fetch(reader: BrokerReader) -> dict:
        portfolio = await reader.get_portfolio(clean_country)
        cash = portfolio.cash
        if cash is None:
            # IOL's holdings endpoint does not report cash, so it comes from the
            # saved account status (a cheap indexed read) instead of a second
            # broker call.
            cash = await _cash_from_account_status(
                db,
                user_id=user_id,
                connection=connection,
                currency=portfolio.currency,
            )
        return portfolio.model_copy(update={"cash": cash}).model_dump(mode="json")

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
    # Market codes are uppercase; the portfolio payload reports them lowercased.
    clean_market = market.strip().upper()
    if not clean_symbol or not clean_market:
        raise BrokerUnavailableError("a market and a symbol are required")

    async def fetch(reader: BrokerReader) -> dict:
        bars = await reader.get_history(symbol=clean_symbol, market=clean_market, days=days)
        return {
            "symbol": clean_symbol,
            "market": clean_market,
            "quotes": [asdict(bar) for bar in bars],
        }

    return await _read(
        db,
        user_id=user_id,
        connection=connection,
        kind=price_history_kind(clean_symbol),
        fetch=fetch,
    )


def bars_from_snapshot(snapshot: Snapshot) -> list[PriceBar]:
    """Read the price bars out of a saved price-history snapshot.

    Snapshots saved before the broker port stored IOL's own quote rows; they
    are recognised by their field names and converted, so old data still loads.
    """
    bars: list[PriceBar] = []
    for item in snapshot.payload.get("quotes", []):
        if "ultimoPrecio" in item or "fechaHora" in item:
            bars.append(price_bar_from_quote(Quote.model_validate(item)))
        else:
            bars.append(PriceBar(**item))
    return bars


#: The name callers used before the port; it now returns neutral bars.
quotes_from_snapshot = bars_from_snapshot


async def load_snapshot(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection, kind: str
) -> Snapshot | None:
    """Read the newest saved snapshot without contacting the broker."""
    return await snapshots_service.latest_snapshot(
        db, user_id=user_id, connection_id=connection.id, kind=kind
    )
